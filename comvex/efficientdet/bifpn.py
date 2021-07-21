from comvex.utils.helpers.functions import name_with_msg
from collections import OrderedDict
from typing import Literal, List, Tuple

import torch
from torch import nn
from torch.nn import functional as F
try:
    from typing_extensions import Final
except:
    from torch.jit import Final

from comvex.utils import SeperateConvXd, XXXConvXdBase


@torch.jit.script
def bifpn_fast_norm(x, weights, dim=0):
    weights = F.relu(weights)
    norm = weights.sum(dim, keepdim=True)

    return x*weights / (norm + 1e-4)


@torch.jit.script
def bifpn_softmax(x, weights, dim=0):
    weights = F.softmax(weights, dim)

    return (x*weights).sum(dim=0)


class BiFPNResizeXd(XXXConvXdBase):
    r"""The `Resize` in equations of Section 3.3 in the official paper.
    Reference from: https://github.com/google/automl/blob/0fb012a80487f0defa4446957c8faf878cd9b75b/efficientdet/efficientdet_arch.py#L55-L95.

    Support 1, 2, or 3D inputs.
    """
    def __init__(
        self,
        in_channel: int,
        out_channel: int,
        in_shape: Tuple[int],
        out_shape: Tuple[int],
        dimension: int = 2,
        upsample_mode: Literal["nearest", "linear", "bilinear", "bicubic", "trilinear"] = "nearest",
        use_bias: bool = False,
        use_batch_norm: bool = False,
        **possible_batch_norm_kwargs
    ) -> None:

        assert (
            len(in_shape) == len(out_shape)
        ), name_with_msg(f"The length of input shape mush be qual to the output one. But got: `in_shape` = {in_shape} and `out_shape` = {out_shape}")

        assert (
            (in_shape[0] > out_shape[0]) == (in_shape[1] > out_shape[1])
        ), name_with_msg(f"`Elements in `in_shape` must be all larger or small than `out_shape`. But got: `in_shape` = {in_shape} and `out_shape` = {out_shape}")

        extra_components = { "max_pool": "AdaptiveMaxPool" } 
        extra_components = { **extra_components, "batch_norm": "BatchNorm" } if use_batch_norm else extra_components
        super().__init__(in_channel, out_channel, dimension, extra_components=extra_components)
        
        if in_shape[0] > out_shape[0]:  # downsampling
            self.interpolate_shape = self.max_pool(out_shape)
        else:  # upsampling
            self.interpolate_shape = nn.Upsample(out_shape, mode=upsample_mode, align_corners=True)

        self.proj_channel = self.conv(in_channel, out_channel, kernel_size=1, use_bias=use_bias)
        self.norm = self.batch_norm(in_channel, **possible_batch_norm_kwargs) if use_batch_norm else None

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.interpolate_shape(x)
        x = self.proj_channel(x)

        if self.norm is not None:
            x = self.norm(x)

        return x


class BiFPNNode(nn.Module):
    r"""
    Referennce from: https://github.com/google/automl/blob/0fb012a80487f0defa4446957c8faf878cd9b75b/efficientdet/efficientdet_arch.py#L418-L475
    """
    def __init__(
        self,
        num_inputs: int,
        in_channel: int,
        out_channel: int,
        in_shape: Tuple[int],
        out_shape: Tuple[int],
        dimension: int = 2,
        upsample_mode: Literal["nearest", "linear", "bilinear", "bicubic", "trilinear"] = "nearest",
        use_bias: bool = False,
        use_batch_norm: bool = False,
        norm_mode: Literal["fast_norm", "softmax", "channel_fast_norm", "channel_softmax"] = "fast_norm",
        **possible_batch_norm_kwargs
    ) -> None:
        super().__init__()

        self.resize = BiFPNResizeXd(
            in_channel,
            out_channel,
            in_shape,
            out_shape,
            dimension,
            upsample_mode,
            use_bias,
            use_batch_norm,
            possible_batch_norm_kwargs
        )
        self.conv = SeperateConvXd(
            in_channel,
            out_channel,
            dimension=dimension,
            **possible_batch_norm_kwargs
        )

        if norm_mode.endswith("fast_norm"):
            self.fuse_features = bifpn_fast_norm
        elif norm_mode.endswith("softmax"):
            self.fuse_features = bifpn_softmax
        else:
            raise ValueError(name_with_msg(f"Unknown `norm_mode`. Got: `norm_mode` = {norm_mode}"))

        if norm_mode.startswith("channel"):
            self.weights = nn.Parameter(torch.ones(num_inputs, 1, out_channel, *([1]*len(in_shape))))  # Ex: (2, 1, C, 1, 1) for images with shape: (B, C, H, W)
        else:
            self.weights = nn.Parameter(torch.ones(num_inputs))


class BiFPNIntermediateNode(BiFPNNode):
    def __init__(
        self,
        **kwargs,
    ) -> None:
        
        super().__init__(num_inputs=2, **kwargs)

    def forward(self, x: torch.Tensor, x_diff: torch.Tensor) -> torch.Tensor:
        x_diff = self.resize(x_diff)
        x_stack = torch.stack([x, x_diff], dim=0)

        return self.conv(self.fuse_features(x_stack, self.weights))


BiFPNOutputEndPoint = BiFPNIntermediateNode  #The start and end nodes in the outputs


class BiFPNOutputNode(BiFPNNode):
    def __init__(
        self,
        **kwargs,
    ) -> None:
        super().__init__(num_inputs=3, **kwargs)

    def forward(self, x: torch.Tensor, x_hidden: torch.Tensor, x_diff: torch.Tensor) -> torch.Tensor:
        x_diff = self.resize(x_diff)
        x_stack = torch.stack([x, x_hidden, x_diff], dim=0)

        return self.conv(self.fuse_features(x_stack, self.weights))


class BiFPNLayer(nn.Module):
    r"""BiFPNLayer
    One block in Figure. 2(d) in the official paper.
    """

    num_nodes: Final[int]

    def __init__(
        self,
        shapes_in_stages: List[Tuple[int]],
        channels_in_stages: List[int],
        **kwargs,
    ) -> None:
        super().__init__()
        
        assert (
            len(shapes_in_stages) == len(channels_in_stages)
        ), name_with_msg(f"The length of `shapes_in_stages` and `channels_in_stages` must be equal. But got: {len(shapes_in_stages)} for stages and {len(shapes_in_stages)} for channels.")

        self.num_nodes = len(shapes_in_stages)

        self.intermediate_nodes = nn.ModuleList(OrderedDict([
            (
                f"intermediate_node_{idx}",
                BiFPNIntermediateNode(
                    in_channel=channels_in_stages[idx + 1],  # Channel of the feature map comes from deeper layers.
                    out_channel=channels_in_stages[idx],
                    in_shape=shapes_in_stages[idx + 1],
                    out_shape=shapes_in_stages[idx],
                    **kwargs
                )
            ) for idx in range(1, self.num_nodes - 1)
        ]))
        self.output_nodes = nn.ModuleList(OrderedDict([
            (
                f"output_node_{idx}",
                BiFPNOutputEndPoint(
                    in_channel=channels_in_stages[idx + 1] if idx == 0 else channels_in_stages[idx - 1],
                    out_channel=channels_in_stages[idx],
                    in_shape=shapes_in_stages[idx + 1] if idx ==0 else shapes_in_stages[idx - 1],
                    out_shape=shapes_in_stages[idx],
                    **kwargs
                ) if idx == 0 or idx == self.num_nodes else BiFPNOutputNode(
                    in_channel=channels_in_stages[idx - 1],  # Channel of the feature map comes from shallower layers.
                    out_channel=channels_in_stages[idx],
                    in_shape=shapes_in_stages[idx - 1],
                    out_shape=shapes_in_stages[idx],
                    **kwargs
                )
            ) for idx in range(self.num_nodes)
        ]))

    def forward(self, feature_list: List[torch.Tensor]) -> List[torch.Tensor]:
        hidden_feature_list = []
        out_feature_list = []
        x_diff = feature_list[-1]  # It will be propagated to every nodes

        # Intermediate Nodes
        for rev_idx, node in reversed(enumerate(self.intermediate_nodes)):
            x = feature_list[rev_idx + 1]  # plus 1 because intermediate nodes don't include shallowest and deepest features
            x_diff = node(x, x_diff)
            hidden_feature_list.append(x_diff)

        # Output Nodes
        for idx, node in enumerate(self.output_nodes):
            x = feature_list[idx]
            if idx == 0 or idx == self.num_nodes:
                x_diff = node(x, x_diff)
            else:
                x_hidden = hidden_feature_list[-(idx + 1)]  # select reversely
                x_diff = node(x, x_hidden, x_diff)

            out_feature_list.append(x_diff)

        return out_feature_list
            

class BiFPN(nn.Module):
    r"""BiFPN from EfficientDet (https://arxiv.org/abs/1911.09070)

    Note: The `feature_list` is assumed to be ordered from shallow to deep features.
    """
    def __init__(
        self,
        num_layers: int,
        shapes_in_stages: List[Tuple[int]],
        channels_in_stages: List[int],
        dimension: int = 2,
        upsample_mode: Literal["nearest", "linear", "bilinear", "bicubic", "trilinear"] = "nearest",
        use_bias: bool = False,
        use_batch_norm: bool = False,
        norm_mode: Literal["fast_norm", "softmax", "channel_fast_norm", "channel_softmax"] = "fast_norm",
        **possible_batch_norm_kwargs
    ) -> None:
        super().__init__()

        self.layers = nn.ModuleList(OrderedDict([
            (
                f"layer_{idx}",
                BiFPNLayer(
                    shapes_in_stages,
                    channels_in_stages,
                    dimension,
                    upsample_mode,
                    use_bias,
                    use_batch_norm,
                    norm_mode,
                    **possible_batch_norm_kwargs
                )
            ) for idx in range(num_layers)
        ]))

    def forward(self, feature_list: List[torch.Tensor]) -> List[torch.Tensor]:
        for layer in self.layers:
            feature_list = layer(feature_list)

        return feature_list