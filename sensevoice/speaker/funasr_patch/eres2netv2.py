# ERes2NetV2 for FunASR — adapted from 3D-Speaker (Apache 2.0)
# Original: https://github.com/modelscope/3D-Speaker

import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from funasr.models.eres2net import pooling_layers
from funasr.models.eres2net.fusion import AFF
from funasr.register import tables


class ReLU(nn.Hardtanh):
    def __init__(self, inplace=False):
        super(ReLU, self).__init__(0, 20, inplace)


class BasicBlockERes2NetV2(nn.Module):
    def __init__(self, in_planes, planes, stride=1, baseWidth=26, scale=2, expansion=2):
        super().__init__()
        width = int(math.floor(planes * (baseWidth / 64.0)))
        self.conv1 = nn.Conv2d(in_planes, width * scale, kernel_size=1, stride=stride, bias=False)
        self.bn1 = nn.BatchNorm2d(width * scale)
        self.nums = scale
        self.expansion = expansion
        convs, bns = [], []
        for _ in range(self.nums):
            convs.append(nn.Conv2d(width, width, kernel_size=3, padding=1, bias=False))
            bns.append(nn.BatchNorm2d(width))
        self.convs = nn.ModuleList(convs)
        self.bns = nn.ModuleList(bns)
        self.relu = ReLU(inplace=True)
        self.conv3 = nn.Conv2d(width * scale, planes * self.expansion, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)
        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes * self.expansion:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes * self.expansion, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * self.expansion),
            )

    def forward(self, x):
        residual = self.shortcut(x)
        out = self.relu(self.bn1(self.conv1(x)))
        spx = torch.split(out, out.shape[1] // self.nums, 1)
        sp = spx[0]
        sp = self.convs[0](sp)
        sp = self.relu(self.bns[0](sp))
        out = sp
        for i in range(1, self.nums):
            sp = spx[i] + sp
            sp = self.convs[i](sp)
            sp = self.relu(self.bns[i](sp))
            out = torch.cat((out, sp), 1)
        out = self.bn3(self.conv3(out))
        out += residual
        return self.relu(out)


class BasicBlockERes2NetV2AFF(nn.Module):
    def __init__(self, in_planes, planes, stride=1, baseWidth=26, scale=2, expansion=2):
        super().__init__()
        width = int(math.floor(planes * (baseWidth / 64.0)))
        self.conv1 = nn.Conv2d(in_planes, width * scale, kernel_size=1, stride=stride, bias=False)
        self.bn1 = nn.BatchNorm2d(width * scale)
        self.nums = scale
        self.expansion = expansion
        convs, bns, fuse_models = [], [], []
        for i in range(self.nums):
            convs.append(nn.Conv2d(width, width, kernel_size=3, padding=1, bias=False))
            bns.append(nn.BatchNorm2d(width))
        for i in range(1, self.nums):
            fuse_models.append(AFF(channels=width))
        self.convs = nn.ModuleList(convs)
        self.bns = nn.ModuleList(bns)
        self.fuse_models = nn.ModuleList(fuse_models)
        self.relu = ReLU(inplace=True)
        self.conv3 = nn.Conv2d(width * scale, planes * self.expansion, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)
        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes * self.expansion:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes * self.expansion, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * self.expansion),
            )

    def forward(self, x):
        residual = self.shortcut(x)
        out = self.relu(self.bn1(self.conv1(x)))
        spx = torch.split(out, out.shape[1] // self.nums, 1)
        sp = spx[0]
        sp = self.convs[0](sp)
        sp = self.relu(self.bns[0](sp))
        out = sp
        for i in range(1, self.nums):
            sp = self.fuse_models[i - 1](sp, spx[i])
            sp = self.convs[i](sp)
            sp = self.relu(self.bns[i](sp))
            out = torch.cat((out, sp), 1)
        out = self.bn3(self.conv3(out))
        out += residual
        return self.relu(out)


@tables.register("model_classes", "ERes2NetV2")
class ERes2NetV2(nn.Module):
    def __init__(self, num_blocks=[3, 4, 6, 3], m_channels=64, feat_dim=80,
                 embedding_size=192, baseWidth=26, scale=2, expansion=2,
                 pooling_func='TSTP', two_emb_layer=False, **kwargs):
        super().__init__()
        block = BasicBlockERes2NetV2
        block_fuse = BasicBlockERes2NetV2AFF
        self.in_planes = m_channels
        self.feat_dim = feat_dim
        self.embedding_size = embedding_size
        self.stats_dim = int(feat_dim / 8) * m_channels * 8
        self.two_emb_layer = two_emb_layer
        self.baseWidth = baseWidth
        self.scale = scale
        self.expansion = expansion

        self.conv1 = nn.Conv2d(1, m_channels, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(m_channels)
        self.layer1 = self._make_layer(block, m_channels, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, m_channels * 2, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block_fuse, m_channels * 4, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block_fuse, m_channels * 8, num_blocks[3], stride=2)
        self.layer3_ds = nn.Conv2d(m_channels * 4 * self.expansion, m_channels * 8 * self.expansion,
                                    kernel_size=3, padding=1, stride=2, bias=False)
        self.fuse34 = AFF(channels=m_channels * 8 * self.expansion, r=4)
        self.n_stats = 1 if pooling_func in ('TAP', 'TSDP') else 2
        self.pool = getattr(pooling_layers, pooling_func)(in_dim=self.stats_dim * self.expansion)
        self.seg_1 = nn.Linear(self.stats_dim * self.expansion * self.n_stats, embedding_size)
        if self.two_emb_layer:
            self.seg_bn_1 = nn.BatchNorm1d(embedding_size, affine=False)
            self.seg_2 = nn.Linear(embedding_size, embedding_size)
        else:
            self.seg_bn_1 = nn.Identity()
            self.seg_2 = nn.Identity()

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for s in strides:
            layers.append(block(self.in_planes, planes, s, baseWidth=self.baseWidth,
                                scale=self.scale, expansion=self.expansion))
            self.in_planes = planes * self.expansion
        return nn.Sequential(*layers)

    def forward(self, x):
        x = x.permute(0, 2, 1)
        x = x.unsqueeze_(1)
        out = F.relu(self.bn1(self.conv1(x)))
        out1 = self.layer1(out)
        out2 = self.layer2(out1)
        out3 = self.layer3(out2)
        out4 = self.layer4(out3)
        out3_ds = self.layer3_ds(out3)
        fuse_out34 = self.fuse34(out4, out3_ds)
        stats = self.pool(fuse_out34)
        embed_a = self.seg_1(stats)
        if self.two_emb_layer:
            out = F.relu(embed_a)
            out = self.seg_bn_1(out)
            return self.seg_2(out)
        return embed_a

    def inference(self, data_in, data_lengths=None, key=None,
                  tokenizer=None, frontend=None, **kwargs):
        import time as _time
        import numpy as np
        from funasr.utils.load_utils import extract_fbank, load_audio_text_image_video
        meta_data = {}
        t1 = _time.perf_counter()
        audio_sample_list = load_audio_text_image_video(
            data_in, fs=16000, audio_fs=kwargs.get("fs", 16000), data_type="sound"
        )
        t2 = _time.perf_counter()
        meta_data["load_data"] = f"{t2 - t1:0.3f}"
        speech, speech_lengths = extract_fbank(
            audio_sample_list, data_type="sound",
            frontend=frontend, is_final=True,
        )
        speech = speech.to(device=kwargs.get("device", "cpu"))
        t3 = _time.perf_counter()
        meta_data["extract_feat"] = f"{t3 - t2:0.3f}"
        with torch.no_grad():
            emb = self.forward(speech.to(torch.float32))
        t4 = _time.perf_counter()
        meta_data["forward"] = f"{t4 - t3:0.3f}"
        results = [{"spk_embedding": emb}]
        return results, meta_data
