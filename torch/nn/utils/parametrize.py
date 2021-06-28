import torch
from torch.nn.modules.container import ModuleList, ModuleDict, Module
from torch.nn.parameter import Parameter
from torch import Tensor

import collections
from contextlib import contextmanager
from typing import Union, Optional, Dict, Tuple, Sequence


_cache_enabled = 0
_cache: Dict[Tuple[int, str], Optional[Tensor]] = {}


@contextmanager
def cached():
    r"""Context manager that enables the caching system within parametrizations
    registered with :func:`register_parametrization`.

    The value of the parametrized objects is computed and cached the first time
    they are required when this context manager is active. The cached values are
    discarded when leaving the context manager.

    This is useful when using a parametrized parameter more than once in the forward pass.
    An example of this is when parametrizing the recurrent kernel of an RNN or when
    sharing weights.

    The simplest way to activate the cache is by wrapping the forward pass of the neural network

    .. code-block:: python

        import torch.nn.utils.parametrize as P
        ...
        with P.cached():
            output = model(inputs)

    in training and evaluation. One may also wrap the parts of the modules that use
    several times the parametrized tensors. For example, the loop of an RNN with a
    parametrized recurrent kernel:

    .. code-block:: python

        with P.cached():
            for x in xs:
                out_rnn = self.rnn_cell(x, out_rnn)
    """
    global _cache
    global _cache_enabled
    _cache_enabled += 1
    try:
        yield
    finally:
        _cache_enabled -= 1
        if not _cache_enabled:
            _cache = {}


def _register_parameter_or_buffer(module, name, X):
    if isinstance(X, Parameter):
        module.register_parameter(name, X)
    else:
        module.register_buffer(name, X)


class ParametrizationList(ModuleList):
    r"""A sequential container that holds and manages the ``original`` or ``original0``, ``original1``, ...
    parameters or buffers of a parametrized :class:`torch.nn.Module`.

    It is the type of ``module.parametrizations[tensor_name]`` when ``module[tensor_name]``
    has been parametrized with :func:`register_parametrization`.

    If the first registered parmetrization has a ``right_inverse`` that returns one tensor or
    does not have a ``right_inverse`` (in which case we assume that ``right_inverse`` is the identity),
    it will hold the tensor under the name ``original``.
    If it has a ``right_inverse`` that returns more than one tensor, these will be registered as
    ``original0``, ``original1``, ...

    .. warning::
        This class is used internally by :func:`register_parametrization`. It is documented
        here for completeness. It shall not be instantiated by the user.

    Args:
        modules (sequence): sequence of modules representing the parametrizations
        original (Parameter or Tensor): parameter or buffer that is parametrized
    """
    original: Tensor

    def __init__(
        self, modules: Sequence[Module], original: Union[Tensor, Parameter]
    ) -> None:
        # We require this because we need to treat differently the first parametrization
        # This should never throw, unless this class is used from the outside
        if len(modules) == 0:
            raise ValueError("ParametrizationList requires one or more modules.")

        super().__init__(modules)

        # In plain words:
        # module.weight must keep its dtype and shape.
        # Furthermore, if there is no right_inverse or the right_inverse returns a tensor,
        # this should be of the same dtype as the original tensor
        #
        # We check that the following invariants hold:
        #    X = module.weight
        #    Y = param.right_inverse(X)
        #    assert isinstance(Y, Tensor) or
        #           (isinstance(Y, collections.abc.Sequence) and all(isinstance(t, Tensor) for t in Y))
        #    Z = param(Y) if isisntance(Y, Tensor) else param(*Y)
        #    # Consistency checks
        #    assert X.dtype == Z.dtype and X.shape == Z.shape
        #    # If it has one input, this allows to be able to use set_ to be able to
        #    # move data to/from the original tensor without changing its id (which is what the
        #    # optimiser uses to track parameters)
        #    if isinstance(Y, Tensor)
        #      assert X.dtype == Y.dtype
        # Below we use original = X, new = Y

        original_shape = original.shape
        original_dtype = original.dtype

        # Compute new
        with torch.no_grad():
            new = original
            for module in reversed(self):  # type: ignore[call-overload]
                if hasattr(module, "right_inverse"):
                    new = module.right_inverse(new)
                # else, we assume that right_inverse is the identity

        if not isinstance(new, Tensor) and not isinstance(new, collections.abc.Sequence):
            raise ValueError("'right_inverse' must return a Tensor or a Sequence of tensors (list, tuple...). "
                             f"Got {type(new).__name__}")

        # Set the number of original tensors
        self.is_tensor = isinstance(new, Tensor)
        self.ntensors = 1 if self.is_tensor else len(new)

        # Register the tensor(s)
        if self.is_tensor:
            if original.dtype != new.dtype:
                raise ValueError(
                    "When `right_inverse` outputs one tensor, it may not change the dtype.\n"
                    f"original.dtype: {original.dtype}\n"
                    f"right_inverse(original).dtype: {new.dtype}"
                )
            # Set the original to original so that the user does not need to re-register the parameter
            # manually in the optimiser
            with torch.no_grad():
                original.set_(new)  # type: ignore[call-overload]
            _register_parameter_or_buffer(self, "original", original)
        else:
            for i, originali in enumerate(new):
                if not isinstance(originali, Tensor):
                    raise ValueError("'right_inverse' must return a Tensor or a Sequence of tensors "
                                     "(list, tuple...). "
                                     f"Got element {i} of the sequence with type {type(originali).__name__}.")

                # If the original tensor was a Parameter that required grad, we expect the user to
                # add the new parameters to the optimizer after registering the parametrization
                # (this is documented)
                if isinstance(original, Parameter):
                    originali = Parameter(originali)
                originali.requires_grad_(original.requires_grad)
                _register_parameter_or_buffer(self, f"original{i}", originali)

        # Consistency checks:
        # Since f : A -> B, right_inverse : B -> A, Z and original should live in B
        # Z = forward(right_inverse(original))
        Z = self()
        if not isinstance(Z, Tensor):
            raise ValueError(
                f"A parametrization must return a tensor. Got {type(Z).__name__}."
            )
        if Z.dtype != original_dtype:
            raise ValueError(
                "Registering a parametrization may not change the dtype of the tensor.\n"
                f"unparametrized dtype: {original_dtype}\n"
                f"parametrized dtype: {Z.dtype}"
            )
        if Z.shape != original_shape:
            raise ValueError(
                "Registering a parametrization may not change the shape of the tensor.\n"
                f"unarametrized shape: {original_shape}\n"
                f"parametrized shape: {Z.shape}"
            )

    def right_inverse(self, value: Tensor) -> None:
        r"""Calls the methods ``right_inverse`` (see :func:`register_parametrization`)
        of the parametrizations in the inverse order they were registered in.
        Then, it stores the result in ``self.original`` if ``right_inverse`` outputs one tensor
        or in ``self.original0``, ``self.original1``, ... if it outputs several.

        Args:
            value (Tensor): Value to which initialize the module
        """
        # All the exceptions in this function should almost never throw.
        # They could throw if, for example, right_inverse function returns a different
        # dtype when given a different input, which should most likely be caused by a
        # bug in the user's code

        with torch.no_grad():
            # See https://github.com/pytorch/pytorch/issues/53103
            for module in reversed(self):  # type: ignore[call-overload]
                if hasattr(module, "right_inverse"):
                    value = module.right_inverse(value)
                # else we assume that right_inverse is the identity
            if self.is_tensor:
                # These exceptions should only throw when a right_inverse function does not
                # return the same dtype for every input, which should most likely be caused by a bug
                if not isinstance(value, Tensor):
                    raise ValueError(
                        f"`right_inverse` should return a tensor. Got {type(value).__name__}"
                    )
                if value.dtype != self.original.dtype:
                    raise ValueError(
                        f"The tensor returned by `right_inverse` has dtype {value.dtype} "
                        f"while `original` has dtype {self.original.dtype}"
                    )
                # We know that the result is going to have the same dtype
                self.original.set_(value)  # type: ignore[call-overload]
            else:
                if not isinstance(value, collections.abc.Sequence):
                    raise ValueError(
                        "'right_inverse' must return a sequence of tensors. "
                        f"Got {type(value).__name__}."
                    )
                if len(value) != self.ntensors:
                    raise ValueError(
                        "'right_inverse' must return a sequence of tensors of length "
                        f"{self.ntensors}. Got a sequence of lenght {len(value)}."
                    )
                for i, tensor in enumerate(value):
                    original_i = getattr(self, f"original{i}")
                    if not isinstance(tensor, Tensor):
                        raise ValueError(
                            f"`right_inverse` must return a sequence of tensors. "
                            f"Got element {i} of type {type(tensor).__name__}"
                        )
                    if original_i.dtype != tensor.dtype:
                        raise ValueError(
                            f"Tensor {i} returned by `right_inverse` has dtype {tensor.dtype} "
                            f"while `original{i}` has dtype {original_i.dtype}"
                        )
                    original_i.set_(tensor)

    def forward(self) -> Tensor:
        # Unpack the originals for the first parametrization
        if self.is_tensor:
            x = self[0](self.original)
        else:
            originals = (getattr(self, f"original{i}") for i in range(self.ntensors))
            x = self[0](*originals)
        # It's not possible to call self[1:] here, so we have to be a bit more cryptic
        for module in list(self._modules.values())[1:]:
            x = module(x)
        return x


def _inject_new_class(module: Module) -> None:
    r"""Sets up a module to be parametrized.

    This works by substituting the class of the module by a class
    that extends it to be able to inject a property

    Args:
        module (nn.Module): module into which to inject the property
    """
    cls = module.__class__

    def getstate(self):
        raise RuntimeError(
            "Serialization of parametrized modules is only "
            "supported through state_dict(). See:\n"
            "https://pytorch.org/tutorials/beginner/saving_loading_models.html"
            "#saving-loading-a-general-checkpoint-for-inference-and-or-resuming-training"
        )

    param_cls = type(
        f"Parametrized{cls.__name__}",
        (cls,),
        {
            "__getstate__": getstate,
        },
    )

    module.__class__ = param_cls


def _inject_property(module: Module, tensor_name: str) -> None:
    r"""Injects a property into module[tensor_name].

    It assumes that the class in the module has already been modified from its
    original one using _inject_new_class and that the tensor under :attr:`tensor_name`
    has already been moved out

    Args:
        module (nn.Module): module into which to inject the property
        tensor_name (str): name of the name of the property to create
    """
    # We check the precondition.
    # This should never fire if register_parametrization is correctly implemented
    assert not hasattr(module, tensor_name)

    def get_parametrized(self) -> Tensor:
        global _cache

        parametrization = self.parametrizations[tensor_name]
        if _cache_enabled:
            key = (id(module), tensor_name)
            tensor = _cache.get(key)
            if tensor is None:
                tensor = parametrization()
                _cache[key] = tensor
            return tensor
        else:
            # If caching is not active, this function just evaluates the parametrization
            return parametrization()

    def set_original(self, value: Tensor) -> None:
        self.parametrizations[tensor_name].right_inverse(value)

    setattr(module.__class__, tensor_name, property(get_parametrized, set_original))

def register_parametrization(
    module: Module, tensor_name: str, parametrization: Module
) -> Module:
    r"""Adds a parametrization to a tensor in a module.

    Assume that ``tensor_name="weight"`` for simplicity. When accessing ``module.weight``,
    the module will return the parametrized version ``parametrization(module.weight)``.
    If the original tensor requires a gradient, the backward pass will differentiate
    through :attr:`parametrization`, and the optimizer will update the tensor accordingly.

    The first time that a module registers a parametrization, this function will add an attribute
    ``parametrizations`` to the module of type :class:`~ParametrizationList`.

    The list of parametrizations on the tensor ``weight`` will be accessible under
    ``module.parametrizations.weight``.

    The original tensor will be accessible under
    ``module.parametrizations.weight.original``.

    Parametrizations may be concatenated by registering several parametrizations
    on the same attribute.

    The training mode of a registered parametrization is updated on registration
    to match the training mode of the host module

    Parametrized parameters and buffers have an inbuilt caching system that can be activated
    using the context manager :func:`cached`.

    A :attr:`parametrization` may optionally implement a method with signature

    .. code-block:: python

        def right_inverse(self, X: Tensor) -> Union[Tensor, Sequence[Tensor]]

    If this method is not implemented, it defaults to the identity.
    This method is called on the unparametrized tensor when the first parametrization
    is registered.

    In most situations, ``right_inverse`` will be a function such that
    ``forward(right_inverse(X)) == X`` (see
    `right inverse <https://en.wikipedia.org/wiki/Inverse_function#Right_inverses>`_).
    Sometimes, when the parametrization is not surjective, it may be reasonable
    to relax this.
    This may be used to initialize the tensor, as shown in the example below.

    It is possible for the first parametrization to depend on several inputs.
    This may be implemented returning a tuple of tensors from ``right_inverse``
    (see the example implementation of a ``RankOne`` parametrization below).

    In this case, the unconstrained tensors are also located under ``module.parametrizations.weight``
    with names ``original0``, ``original1``,...

    .. note::

        Whenever a parametrization is registered, both its forward and backward method will be called
        once to perform a number of consistency checks.

    .. warning::

        If a parametrization depends on several inputs, :func:`~register_parametrization`
        will register a number of new parameters. If such parametrization is registered
        after the optimizer is created, these new parameters will need to be added manually
        to the optimizer. See :meth:`torch.Optimizer.add_param_group`.

    Args:
        module (nn.Module): module on which to register the parametrization
        tensor_name (str): name of the parameter or buffer on which to register
            the parametrization
        parametrization (nn.Module): the parametrization to register

    Raises:
        ValueError: if the module does not have a parameter or a buffer named :attr:`tensor_name`

    Examples:
        >>> import torch
        >>> import torch.nn as nn
        >>> import torch.nn.utils.parametrize as P
        >>>
        >>> class Symmetric(nn.Module):
        >>>     def forward(self, X):
        >>>         return X.triu() + X.triu(1).T  # Return a symmetric matrix
        >>>
        >>>     def right_inverse(self, A):
        >>>         return A.triu()
        >>>
        >>> m = nn.Linear(5, 5)
        >>> P.register_parametrization(m, "weight", Symmetric())
        >>> print(torch.allclose(m.weight, m.weight.T))  # m.weight is now symmetric
        True
        >>> A = torch.rand(5, 5)
        >>> A = A + A.T   # A is now symmetric
        >>> m.weight = A  # Initialize the weight to be the symmetric matrix A
        >>> print(torch.allclose(m.weight, A))
        True

        >>> class RankOne(nn.Module):
        >>>     def forward(self, x, y):
        >>>         # Form a rank 1 matrix multiplying two vectors
        >>>         return x.unsqueeze(-1) @ y.unsqueeze(-2)
        >>>
        >>>     def right_inverse(self, Z):
        >>>         # Project Z onto the rank 1 matrices
        >>>         U, S, Vh = torch.linalg.svd(Z, full_matrices=False)
        >>>         # Return rescaled singular vectors
        >>>         s0_sqrt = S[0].sqrt().unsqueeze(-1)
        >>>         return U[..., :, 0] * s0_sqrt, Vh[..., 0, :] * s0_sqrt
        >>>
        >>> linear_rank_one = P.register_parametrization(nn.Linear(4, 4), "weight", RankOne())
        >>> print(torch.linalg.matrix_rank(linear_rank_one.weight).item())
        1

    """
    parametrization.train(module.training)
    if is_parametrized(module, tensor_name):
        # Correctness checks.
        # If A is the space of tensors with shape and dtype equal to module.weight
        # we check that parametrization.forward and parametrization.right_inverse are
        # functions from A to A

        Y = getattr(module, tensor_name)
        X = parametrization(Y)
        if not isinstance(X, Tensor):
            raise ValueError(
                f"A parametrization must return a tensor. Got {type(X).__name__}."
            )
        if X.dtype != Y.dtype:
            raise ValueError(
                "Registering a parametrization may not change the dtype of the tensor.\n"
                f"module.{tensor_name}.dtype: {Y.dtype}\n"
                f"parametrization(module.{tensor_name}).dtype: {X.dtype}"
            )
        if X.shape != Y.shape:
            raise ValueError(
                "Registering a parametrization may not change the shape of the tensor.\n"
                f"module.{tensor_name}.shape: {Y.shape}\n"
                f"parametrization(module.{tensor_name}).shape: {X.shape}"
            )
        if hasattr(parametrization, "right_inverse"):
            Z = parametrization.right_inverse(X)  # type: ignore[operator]
            if not isinstance(Z, Tensor):
                raise ValueError(
                    f"parametrization.right_inverse must return a tensor. Got: {type(Z).__name__}"
                )
            if Z.dtype != Y.dtype:
                raise ValueError(
                    "The tensor returned by parametrization.right_inverse must have the same dtype "
                    f"as module.{tensor_name}.\n"
                    f"module.{tensor_name}.dtype: {Y.dtype}\n"
                    f"returned dtype: {Z.dtype}"
                )
            if Z.shape != Y.shape:
                raise ValueError(
                    "The tensor returned by parametrization.right_inverse must have the same shape "
                    f"as module.{tensor_name}.\n"
                    f"module.{tensor_name}.shape: {Y.shape}\n"
                    f"returned shape: {Z.shape}"
                )
        # else right_inverse is assumed to be the identity

        # add the new parametrization to the parametrization list
        assert isinstance(module.parametrizations, ModuleDict)  # Make mypy happy
        module.parametrizations[tensor_name].append(parametrization)
    elif tensor_name in module._buffers or tensor_name in module._parameters:
        # Set the parametrization mechanism
        # Fetch the original buffer or parameter
        original = getattr(module, tensor_name)
        # We create this early to check for possible errors
        parametrizations = ParametrizationList([parametrization], original)
        # Delete the previous parameter or buffer
        delattr(module, tensor_name)
        # If this is the first parametrization registered on the module,
        # we prepare the module to inject the property
        if not is_parametrized(module):
            # Change the class
            _inject_new_class(module)
            # Inject a ``ModuleDict`` into the instance under module.parametrizations
            module.parametrizations = ModuleDict()
        # Add a property into the class
        _inject_property(module, tensor_name)
        # Add a ParametrizationList
        assert isinstance(module.parametrizations, ModuleDict)  # Make mypy happy
        module.parametrizations[tensor_name] = parametrizations
    else:
        raise ValueError(
            f"Module '{module}' does not have a parameter, a buffer, or a "
            f"parametrized element with name '{tensor_name}'"
        )
    return module


def is_parametrized(module: Module, tensor_name: Optional[str] = None) -> bool:
    r"""Returns ``True`` if module has an active parametrization.

    If the argument :attr:`tensor_name` is specified, returns ``True`` if
    ``module[tensor_name]`` is parametrized.

    Args:
        module (nn.Module): module to query
        name (str, optional): attribute in the module to query
            Default: ``None``
    """
    parametrizations = getattr(module, "parametrizations", None)
    if parametrizations is None or not isinstance(parametrizations, ModuleDict):
        return False
    if tensor_name is None:
        # Check that there is at least one parametrized buffer or Parameter
        return len(parametrizations) > 0
    else:
        return tensor_name in parametrizations


def remove_parametrizations(
    module: Module, tensor_name: str, leave_parametrized: bool = True
) -> Module:
    r"""Removes the parametrizations on a tensor in a module.

    - If ``leave_parametrized=True``, ``module[tensor_name]`` will be set to
      its current output. In this case, the parametrization shall not change the ``dtype``
      of the tensor.
    - If ``leave_parametrized=False``, ``module[tensor_name]`` will be set to
      the unparametrised tensor in ``module.parametrizations[tensor_name].original``.
      This is only possible when the parametrization depends on just one tensor.

    Args:
        module (nn.Module): module from which remove the parametrization
        tensor_name (str): name of the parametrization to be removed
        leave_parametrized (bool, optional): leave the attribute :attr:`tensor_name` parametrized.
            Default: ``True``

    Returns:
        Module: module

    Raises:
        ValueError: if ``module[tensor_name]`` is not parametrized
        ValueError: if ``leave_parametrized=False`` and the parametrization depends on several tensors
    """

    if not is_parametrized(module, tensor_name):
        raise ValueError(f"Module {module} does not have a parametrization on {tensor_name}")

    # Fetch the original tensor
    assert isinstance(module.parametrizations, ModuleDict)  # Make mypy happy
    parametrizations = module.parametrizations[tensor_name]
    if parametrizations.is_tensor:
        original = parametrizations.original
        if leave_parametrized:
            with torch.no_grad():
                t = getattr(module, tensor_name)
            # We know they have the same dtype because we have checked this when registering the
            # parametrizations. As such, we can use set_
            # We do this so that the parameter does not to change the id()
            # This way the user does not need to update the optimizer
            with torch.no_grad():
                original.set_(t)
    else:
        if leave_parametrized:
            # We cannot use no_grad because we need to know whether one or more
            # original tensors required grad
            t = getattr(module, tensor_name)
            # We'll have to trust the user to add it to the optimizer
            original = Parameter(t) if t.requires_grad else t
        else:
            raise ValueError("Cannot leave unparametrized (`leave_parametrized=False`) a tensor "
                             "that is parametrized in terms of a sequence of tensors.")

    # Delete the property that manages the parametrization
    delattr(module.__class__, tensor_name)
    # Delete the ParametrizationList
    del module.parametrizations[tensor_name]

    # Restore the parameter / buffer into the main class
    _register_parameter_or_buffer(module, tensor_name, original)

    # Roll back the parametrized class if no other buffer or parameter
    # is currently parametrized in this class
    if not is_parametrized(module):
        delattr(module, "parametrizations")
        # Restore class
        orig_cls = module.__class__.__bases__[0]
        module.__class__ = orig_cls
    return module
