import torch
from torch import nn
from einops.layers.torch import Rearrange, Reduce

from models.vit import ViTBase
from models.transformer import Transformer
from models.utils import FeedForward, UNetBase, UNetDecoder, ResNetFullPreActivationBottleneck


class TransUNetViT(ViTBase):
    def __init__(
        self,
        image_size=224,
        image_channel=3,
        patch_size=16,   # one lateral's size of a squre patch
        dim=512,
        num_heads=4,
        num_layers=12,
        dropout=0.0,
        *,
        feedforward_dim=None,
        self_defined_transformer=None,
        ):
        super().__init__(image_size, patch_size, dim, num_heads, image_channel)

        self.proj_patches = nn.Sequential(
            Rearrange(x, "b c (h p) (w p_) -> b (h w) (p p_ c)", p=self.patch_size, p_=self.patch_size),
            nn.Linear(self.patch_dim, self.dim, bias=False)
        )

        self.position_code = nn.Parameter(torch.randn(1, self.num_patches, self.patch_dim))
        self.token_dropout = nn.Dropout(dropout)

        feedforward_dim = feedforward_dim if feedforward_dim is not None else 2*self.dim

        self.transformer = (
            self_defined_transformer
            if self_defined_transformer is not None
            else Transformer(
                dim=dim, 
                heads=num_heads,
                depth=num_layers,
                ff_dim=feedforward_dim,
                ff_dropout=dropout,
                max_seq_len=self.num_patches
            )
        )

    def forward(self, x, att_mask=None, padding_mask=None):
        b, c, h, w, p = *x.shape, self.num_patches

        # Images patching and projection
        x = self.proj_patches(x)

        # Add position code
        x = x + self.position_code
        
        # Token dropout
        x = self.token_dropout(x)
        
        # Transformer
        x = self.transformer(x)
        
        return x


class TransUNetEncoder(nn.Module):
    def __init__(self, input_channel, channel_in_between, image_size, dim, num_heads, num_layers, ff_dim, ff_dropout):
        super().__init__()
        
        assert len(channel_in_between) >= 1, f"[{self.__class__.__name__}] Please specify the number of channels for at least 1 layer."
        vit_image_size = image_size*(2**(-len(channel_in_between)))

        channel_in_between = [input_channel] + channel_in_between
        self.layers = nn.ModuleList([
            ResNetFullPreActivationBottleneck(channel_in_between[idx], channel_in_between[idx + 1])
            for idx in range(len(channel_in_between) - 1)
        ])
        self.vit = TransUNetViT(
            image_size=vit_image_size,
            patch_size=16,
            image_channel=256,
            dim=dim,
            num_heads=num_heads,
            num_layers=num_layers,
            feedforward_dim=ff_dim,
            dropout=ff_dropout
        )

    def forward(self, x):
        hidden_xs = []
        for convBlock in self.layers:
            x = convBlock(x)
            hidden_xs.append(x)

        x = self.vit(x)

        return x, hidden_xs


class TransUNet(UNetBase):
    """
    Architecture:
        encoder               decoder --> output_layer
           |       .......       ^ 
           |                     |
             ->  middle_layer --
    """
    def __init__(
        self,
        input_channel, 
        middle_channel, 
        output_channel, 
        channel_in_between=[],
        image_size=224,
        patch_size=16,
        dim=512,
        num_heads=16,
        num_layers=12,
        feedforward_dim=2048,
        dropout=0,
        to_remain_size=False
    ):
        super().__init__(channel_in_between=channel_in_between, to_remain_size=to_remain_size)

        self.encoder = TransUNetEncoder(
            input_channel, 
            self.channel_in_between,
            image_size,
            dim,
            num_heads,
            num_layers,
            feedforward_dim,
            dropout
        )
        self.middle_layer = Rearrange("b (p q) d -> b d p q", p=patch_size, q=patch_size)
        self.decoder = UNetDecoder(middle_channel, self.channel_in_between[::-1])
        self.output_layer = nn.Conv2d(self.channel_in_between[0], output_channel, kernel_size=1)  # kernel_size == 3 in the offical code

    def forward(self, x):
        b, c, h, w = x.shape

        x, hidden_xs = self.encoder(x)
        x = self.middle_layer(x)
        x = self.decoder(x, hidden_xs[::-1])
        x = self.output_layer(x)
        
        if self.to_remain_size:
            x = nn.functional.interpolate(
                x, 
                self.image_size if self.image_size is not None else (h, w)
            )
            
        return x


if __name__ == "__main__":
    transUnet = TransUNet(
        input_channel=3,
        middle_channel=1024,
        output_channel=10,
        channel_in_between=[64, 128, 256],
        image_size=224,
        patch_size=16,
        dim=512,
        num_heads=16,
        num_layers=12,
        feedforward_dim=2048,
        dropout=0,
        to_remain_size=True
    )
    print(transUnet)

    x = torch.randn(1, 3, 224, 224)

    print(transUnet(x).shape)