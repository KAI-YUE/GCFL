"""
Refer to grace: https://github.com/sands-lab/grace
"""
from abc import ABC, abstractmethod

class Compressor(ABC):
    """Interface for compressing and decompressing a given tensor."""

    def __init__(self):
        self._require_grad_idx = False

    @abstractmethod
    def compress(self, tensor, compress_ctx):
        """Compresses a tensor with the given compression context, and then returns it with the context needed to decompress it."""

    @abstractmethod
    def decompress(self, tensors, decompress_ctx):
        """Decompress the tensor with the given decompression context."""

    @abstractmethod
    def compress_ratio(self):
        """Estimated compression ratio of the compressor."""

    def reset(self):
        """Reset the status."""

    def trans_aggregation(self, tensor):
        """Transform a raw aggregation sum."""

    def aggregate(self, tensors):
        """Aggregate a list of tensors."""
        return sum(tensors)


from grace_fl.signSGD import SignSGDCompressor
from grace_fl.pred_signSGD import PredSignSGDCompressor
from grace_fl.pred_RLE_signSGD import PredRLESignSGDCompressor
from grace_fl.ideal_pred_signSGD import IdealBinaryPredSignSGDCompressor

compressor_registry = {
"signSGD": SignSGDCompressor,
"pred_signSGD": PredSignSGDCompressor,
"ideal_pred_signSGD": IdealBinaryPredSignSGDCompressor,
"pred_rle_signSGD": PredRLESignSGDCompressor
}
