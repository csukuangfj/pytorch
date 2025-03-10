from functools import reduce
from typing import Tuple

import torch
import torch.nn.functional as F

from .base_sparsifier import BaseSparsifier

def _flat_idx_to_2d(idx, shape):
    rows = idx // shape[1]
    cols = idx % shape[1]
    return rows, cols

class WeightNormSparsifier(BaseSparsifier):
    r"""Weight-Norm Sparsifier

    This sparsifier computes the norm of every sparse block and "zeroes-out" the
    ones with the lowest norm. The level of sparsity defines how many of the
    blocks is removed.

    This sparsifier is controlled by three variables:
    1. `sparsity_level` defines the number of *sparse blocks* that are zeroed-out
    2. `sparse_block_shape` defines the shape of the sparse blocks. Note that
        the sparse blocks originate at the zero-index of the tensor.
    3. `zeros_per_block` is the number of zeros that we are expecting in each
        sparse block. By default we assume that all elements within a block are
        zeroed-out. However, setting this variable sets the target number of
        zeros per block. The zeros within each block are chosen as the *smallest
        absolute values*.

    Args:

        sparsity_level: The target level of sparsity
        sparse_block_shape: The shape of a sparse block
        zeros_per_block: Number of zeros in a sparse block

    Note::
        All arguments to the WeightNormSparsifier constructor are "default"
        arguments and could be overriden by the configuration provided in the
        `prepare` step.
    """
    def __init__(self,
                 sparsity_level: float = 0.5,
                 sparse_block_shape: Tuple[int, int] = (1, 4),
                 zeros_per_block: int = None):
        if zeros_per_block is None:
            zeros_per_block = reduce((lambda x, y: x * y), sparse_block_shape)
        defaults = {
            'sparsity_level': sparsity_level,
            'sparse_block_shape': sparse_block_shape,
            'zeros_per_block': zeros_per_block
        }
        super().__init__(defaults=defaults)

    def update_mask(self, layer, sparsity_level, sparse_block_shape,
                    zeros_per_block, **kwargs):
        if zeros_per_block != reduce((lambda x, y: x * y), sparse_block_shape):
            raise NotImplementedError('Partial block sparsity is not yet there')
        # TODO: Add support for multiple parametrizations for the same weight
        mask = layer.parametrizations.weight[0].mask
        if sparsity_level <= 0:
            mask.data = torch.ones(layer.weight.shape, device=layer.weight.device)
        elif sparsity_level >= 1.0:
            mask.data = torch.zeros(layer.weight.shape, device=layer.weight.device)
        else:
            ww = layer.weight * layer.weight
            ww_reshaped = ww.reshape(1, *ww.shape)
            ww_pool = F.avg_pool2d(ww_reshaped, kernel_size=sparse_block_shape,
                                   stride=sparse_block_shape, ceil_mode=True)
            ww_pool_flat = ww_pool.flatten()
            _, sorted_idx = torch.sort(ww_pool_flat)
            threshold_idx = int(round(sparsity_level * len(sorted_idx)))
            sorted_idx = sorted_idx[:threshold_idx]
            rows, cols = _flat_idx_to_2d(sorted_idx, ww_pool.shape[1:])
            rows *= sparse_block_shape[0]
            cols *= sparse_block_shape[1]

            new_mask = torch.ones(ww.shape, device=layer.weight.device)
            for row, col in zip(rows, cols):
                new_mask[row:row + sparse_block_shape[0],
                         col:col + sparse_block_shape[1]] = 0
            mask.data = new_mask
