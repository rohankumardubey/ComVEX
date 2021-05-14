import torch
from torch import nn

class Residual(nn.Module):
    def __init__(self, fn, skip_connection_dropout=0.0, skip_connection_dropout_module=None):
        super(Residual, self).__init__()

        assert fn is not None, f"[{self.__class__.__name__}] Must give it a function (normaly, a neural net)"
        self._fn = fn
        self.skip_connection_dropout = skip_connection_dropout_module(skip_connection_dropout) if skip_connection_dropout_module is not None else nn.Dropout(skip_connection_dropout)

    def forward(self, x, *args, **kwargs):
        if isinstance(x, tuple) or isinstance(x, list):
            return self._fn(x, *args, **kwargs) + self.skip_connection_dropout(x[0])  # assume the first element is the main hidden state
        else:
            return self._fn(x, *args, **kwargs) + self.skip_connection_dropout(x)

# TODO: Refine it!!
class LayerNorm(nn.Module):
    def __init__(self, fn=None, *, dim=None, use_pre_norm=False, use_cross_attention=False, cross_dim=None):
        super(LayerNorm, self).__init__()
        self._fn = fn
        self._use_pre_norm = use_pre_norm
        self._use_cross_attention = use_cross_attention
        if self._fn:
            self._norm = nn.LayerNorm(dim)
            if self._use_pre_norm and use_cross_attention:
                assert cross_dim is not None, f"[{self.__class__.__name__}] Please specify `cross_dim` when using cross attention"
                self._cross_norm_k = nn.LayerNorm(cross_dim)
                self._cross_norm_v = nn.LayerNorm(cross_dim)

    def forward(self, x, *args, **kwargs):
        if self._fn:
            if self._use_pre_norm:
                if self._use_cross_attention:
                    x = self._norm(x[0]), self._cross_norm_k(x[1]), self._cross_norm_v(x[2])
                    return self._fn(x, *args, **kwargs)
                return self._fn(self._norm(x), *args, **kwargs)
            else:
                return self._norm(self._fn(x, *args, **kwargs))

        return self._norm(x)


class MaskLayerNorm(LayerNorm):
    """
    Args:
        x (b, n, d): input tensor
        norm_mask (b, n) (Bool Tensor): True => to 0, False => ignore 
    """

    def forward(self, x, norm_mask=None, *args, **kwargs):
        if self._fn:
            if self._use_pre_norm:
                x = self._fn(self._norm(x), *args, **kwargs)
            else:
                x = self._norm(self._fn(x, *args, **kwargs))
        else:
            x = self._norm(x)

        assert norm_mask is not None, f"[{self.__class__.__name__}] Please provide `norm_mask`."

        return x.masked_fill_(norm_mask, 0)


class FeedForward(nn.Module):
    def __init__(self, *, dim=None, hidden_dim=None, output_dim=None, ff_dim_scale=None, ff_dropout=0.0, useNorm=False, **kwargs):
        super().__init__()
        assert dim is not None, f"[{self.__class__.__name__}] Must specify the input dim"
        if hidden_dim is None:
            assert ff_dim_scale is not None, f"[{self.__class__.__name__}] Must specify `ff_dim_scale` when `hidden_dim` doesn't exist"

        hidden_dim = hidden_dim if hidden_dim is not None else ff_dim_scale*dim
        out_dim = output_dim if output_dim is not None else dim

        if useNorm:
            self._net = nn.Sequential(
                LayerNorm(
                    nn.Sequential(
                        nn.Linear(dim, hidden_dim),
                        nn.Dropout(ff_dropout),
                    ),
                    dim=dim
                ),
                nn.GELU(),
                LayerNorm(
                    nn.Linear(hidden_dim, out_dim),
                    dim=hidden_dim
                ),
            )
        else:
            self._net = nn.Sequential(
                nn.Linear(dim, hidden_dim),
                nn.GELU(),
                nn.Dropout(ff_dropout),
                nn.Linear(hidden_dim, out_dim),
            )

    def forward(self, x):
        return self._net(x)