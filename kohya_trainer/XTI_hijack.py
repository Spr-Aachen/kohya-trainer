import torch
from typing import Union, List, Optional, Dict, Any, Tuple
from diffusers.models.unet_2d_condition import UNet2DConditionOutput

import sys
from pathlib import Path
parentDir = Path(__file__).absolute().parent.parent.as_posix()
sys.path.append(parentDir)

from kohya_trainer.library.original_unet import SampleOutput


def unet_forward_XTI(
    self,
    sample: torch.FloatTensor,
    timestep: Union[torch.Tensor, float, int],
    encoder_hidden_states: torch.Tensor,
    class_labels: Optional[torch.Tensor] = None,
    return_dict: bool = True,
) -> Union[Dict, Tuple]:
    r"""
    Args:
        sample (`torch.FloatTensor`): (batch, channel, height, width) noisy inputs tensor
        timestep (`torch.FloatTensor` or `float` or `int`): (batch) timesteps
        encoder_hidden_states (`torch.FloatTensor`): (batch, sequence_length, feature_dim) encoder hidden states
        return_dict (`bool`, *optional*, defaults to `True`):
            Whether or not to return a dict instead of a plain tuple.

    Returns:
        `SampleOutput` or `tuple`:
        `SampleOutput` if `return_dict` is True, otherwise a `tuple`. When returning a tuple, the first element is the sample tensor.
    """
    # By default samples have to be AT least a multiple of the overall upsampling factor.
    # The overall upsampling factor is equal to 2 ** (# num of upsampling layears).
    # However, the upsampling interpolation output size can be forced to fit any upsampling size
    # on the fly if necessary.
    # デフォルトではサンプルは「2^アップサンプルの数」、つまり64の倍数である必要がある
    # ただそれ以外のサイズにも対応できるように、必要ならアップサンプルのサイズを変更する
    # 多分画質が悪くなるので、64で割り切れるようにしておくのが良い
    default_overall_up_factor = 2**self.num_upsamplers

    # upsample size should be forwarded when sample is not a multiple of `default_overall_up_factor`
    # 64で割り切れないときはupsamplerにサイズを伝える
    forward_upsample_size = False
    upsample_size = None

    if any(s % default_overall_up_factor != 0 for s in sample.shape[-2:]):
        # logger.info("Forward upsample size to force interpolation output size.")
        forward_upsample_size = True

    # 1. time
    timesteps = timestep
    timesteps = self.handle_unusual_timesteps(sample, timesteps)  # 変な時だけ処理

    t_emb = self.time_proj(timesteps)

    # timesteps does not contain any weights and will always return f32 tensors
    # but time_embedding might actually be running in fp16. so we need to cast here.
    # there might be better ways to encapsulate this.
    # timestepsは重みを含まないので常にfloat32のテンソルを返す
    # しかしtime_embeddingはfp16で動いているかもしれないので、ここでキャストする必要がある
    # time_projでキャストしておけばいいんじゃね？
    t_emb = t_emb.to(dtype=self.dtype)
    emb = self.time_embedding(t_emb)

    # 2. pre-process
    sample = self.conv_in(sample)

    # 3. down
    down_block_res_samples = (sample,)
    down_i = 0
    for downsample_block in self.down_blocks:
        # downblockはforwardで必ずencoder_hidden_statesを受け取るようにしても良さそうだけど、
        # まあこちらのほうがわかりやすいかもしれない
        if downsample_block.has_cross_attention:
            sample, res_samples = downsample_block(
                hidden_states=sample,
                temb=emb,
                encoder_hidden_states=encoder_hidden_states[down_i : down_i + 2],
            )
            down_i += 2
        else:
            sample, res_samples = downsample_block(hidden_states=sample, temb=emb)

        down_block_res_samples += res_samples

    # 4. mid
    sample = self.mid_block(sample, emb, encoder_hidden_states=encoder_hidden_states[6])

    # 5. up
    up_i = 7
    for i, upsample_block in enumerate(self.up_blocks):
        is_final_block = i == len(self.up_blocks) - 1

        res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
        down_block_res_samples = down_block_res_samples[: -len(upsample_block.resnets)]  # skip connection

        # if we have not reached the final block and need to forward the upsample size, we do it here
        # 前述のように最後のブロック以外ではupsample_sizeを伝える
        if not is_final_block and forward_upsample_size:
            upsample_size = down_block_res_samples[-1].shape[2:]

        if upsample_block.has_cross_attention:
            sample = upsample_block(
                hidden_states=sample,
                temb=emb,
                res_hidden_states_tuple=res_samples,
                encoder_hidden_states=encoder_hidden_states[up_i : up_i + 3],
                upsample_size=upsample_size,
            )
            up_i += 3
        else:
            sample = upsample_block(
                hidden_states=sample, temb=emb, res_hidden_states_tuple=res_samples, upsample_size=upsample_size
            )

    # 6. post-process
    sample = self.conv_norm_out(sample)
    sample = self.conv_act(sample)
    sample = self.conv_out(sample)

    if not return_dict:
        return (sample,)

    return SampleOutput(sample=sample)


def downblock_forward_XTI(
    self, hidden_states, temb=None, encoder_hidden_states=None, attention_mask=None, cross_attention_kwargs=None
):
    output_states = ()
    i = 0

    for resnet, attn in zip(self.resnets, self.attentions):
        if self.training and self.gradient_checkpointing:

            def create_custom_forward(module, return_dict=None):
                def custom_forward(*inputs):
                    if return_dict is not None:
                        return module(*inputs, return_dict=return_dict)
                    else:
                        return module(*inputs)

                return custom_forward

            hidden_states = torch.utils.checkpoint.checkpoint(create_custom_forward(resnet), hidden_states, temb)
            hidden_states = torch.utils.checkpoint.checkpoint(
                create_custom_forward(attn, return_dict=False), hidden_states, encoder_hidden_states[i]
            )[0]
        else:
            hidden_states = resnet(hidden_states, temb)
            hidden_states = attn(hidden_states, encoder_hidden_states=encoder_hidden_states[i]).sample

        output_states += (hidden_states,)
        i += 1

    if self.downsamplers is not None:
        for downsampler in self.downsamplers:
            hidden_states = downsampler(hidden_states)

        output_states += (hidden_states,)

    return hidden_states, output_states


def upblock_forward_XTI(
    self,
    hidden_states,
    res_hidden_states_tuple,
    temb=None,
    encoder_hidden_states=None,
    upsample_size=None,
):
    i = 0
    for resnet, attn in zip(self.resnets, self.attentions):
        # pop res hidden states
        res_hidden_states = res_hidden_states_tuple[-1]
        res_hidden_states_tuple = res_hidden_states_tuple[:-1]
        hidden_states = torch.cat([hidden_states, res_hidden_states], dim=1)

        if self.training and self.gradient_checkpointing:

            def create_custom_forward(module, return_dict=None):
                def custom_forward(*inputs):
                    if return_dict is not None:
                        return module(*inputs, return_dict=return_dict)
                    else:
                        return module(*inputs)

                return custom_forward

            hidden_states = torch.utils.checkpoint.checkpoint(create_custom_forward(resnet), hidden_states, temb)
            hidden_states = torch.utils.checkpoint.checkpoint(
                create_custom_forward(attn, return_dict=False), hidden_states, encoder_hidden_states[i]
            )[0]
        else:
            hidden_states = resnet(hidden_states, temb)
            hidden_states = attn(hidden_states, encoder_hidden_states=encoder_hidden_states[i]).sample

        i += 1

    if self.upsamplers is not None:
        for upsampler in self.upsamplers:
            hidden_states = upsampler(hidden_states, upsample_size)

    return hidden_states
