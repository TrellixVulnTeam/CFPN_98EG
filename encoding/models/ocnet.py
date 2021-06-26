from __future__ import division

import torch
import torch.nn as nn
import torch.nn.functional as F

from .fcn import FCNHead
from .base import BaseNet

__all__ = ['ocnet', 'get_ocnet']

class ocnet(BaseNet):
    def __init__(self, nclass, backbone, aux=True, se_loss=False, norm_layer=nn.BatchNorm2d, **kwargs):
        super(ocnet, self).__init__(nclass, backbone, aux, se_loss, norm_layer=norm_layer, **kwargs)

        self.head = ocnetHead(2048, nclass, norm_layer, up_kwargs=self._up_kwargs)
        if aux:
            self.auxlayer = FCNHead(1024, nclass, norm_layer)

    def forward(self, x):
        imsize = x.size()[2:]
        c1, c2, c3, c4 = self.base_forward(x)
        x = self.head(c1,c2,c3,c4)
        x = F.interpolate(x, imsize, **self._up_kwargs)
        outputs = [x]
        if self.aux:
            auxout = self.auxlayer(c3)
            auxout = F.interpolate(auxout, imsize, **self._up_kwargs)
            outputs.append(auxout)
        return tuple(outputs)

class ocnetHead(nn.Module):
    def __init__(self, in_channels, out_channels, norm_layer=nn.BatchNorm2d, **kwargs):
        super(ocnetHead, self).__init__()
        oc_arch="asp"
        if oc_arch == 'base':
            self.context = nn.Sequential(
                nn.Conv2d(2048, 512, 3, 1, padding=1, bias=False),
                norm_layer(512),
                nn.ReLU(True),
                BaseOCModule(512, 512, 256, 256, scales=([1]), norm_layer=norm_layer, **kwargs))
        elif oc_arch == 'pyramid':
            self.context = nn.Sequential(
                nn.Conv2d(2048, 512, 3, 1, padding=1, bias=False),
                norm_layer(512),
                nn.ReLU(True),
                PyramidOCModule(512, 512, 256, 512, scales=([1, 2, 3, 6]), norm_layer=norm_layer, **kwargs))
        elif oc_arch == 'asp':
            self.context = ASPOCModule(2048, 512, 256, 512, norm_layer=norm_layer, **kwargs)
        else:
            raise ValueError("Unknown OC architecture!")

        self.out = nn.Sequential(nn.Dropout2d(0.1), nn.Conv2d(512, out_channels, 1))

    def forward(self, c1, c2, c3, c4):
        x = self.context(c4)
        return self.out(x)
    
def get_ocnet(dataset='pascal_voc', backbone='resnet50', pretrained=False,
                 root='~/.encoding/models', **kwargs):
    # infer number of classes
    from ..datasets import datasets
    model = ocnet(datasets[dataset.lower()].NUM_CLASS, backbone=backbone, root=root, **kwargs)
    if pretrained:
        raise NotImplementedError

    return model





class BaseAttentionBlock(nn.Module):
    """The basic implementation for self-attention block/non-local block."""

    def __init__(self, in_channels, out_channels, key_channels, value_channels,
                 scale=1, norm_layer=nn.BatchNorm2d, **kwargs):
        super(BaseAttentionBlock, self).__init__()
        self.scale = scale
        self.key_channels = key_channels
        self.value_channels = value_channels
        if scale > 1:
            self.pool = nn.MaxPool2d(scale)

        self.f_value = nn.Conv2d(in_channels, value_channels, 1)
        self.f_key = nn.Sequential(
            nn.Conv2d(in_channels, key_channels, 1),
            norm_layer(key_channels),
            nn.ReLU(True)
        )
        self.f_query = self.f_key
        self.W = nn.Conv2d(value_channels, out_channels, 1)
        nn.init.constant_(self.W.weight, 0)
        nn.init.constant_(self.W.bias, 0)

    def forward(self, x):
        batch_size, c, w, h = x.size()
        if self.scale > 1:
            x = self.pool(x)

        value = self.f_value(x).view(batch_size, self.value_channels, -1).permute(0, 2, 1)
        query = self.f_query(x).view(batch_size, self.key_channels, -1).permute(0, 2, 1)
        key = self.f_key(x).view(batch_size, self.key_channels, -1)

        sim_map = torch.bmm(query, key) * (self.key_channels ** -.5)
        sim_map = F.softmax(sim_map, dim=-1)

        context = torch.bmm(sim_map, value).permute(0, 2, 1).contiguous()
        context = context.view(batch_size, self.value_channels, *x.size()[2:])
        context = self.W(context)
        if self.scale > 1:
            context = F.interpolate(context, size=(w, h), mode='bilinear', align_corners=True)

        return context


class BaseOCModule(nn.Module):
    """Base-OC"""

    def __init__(self, in_channels, out_channels, key_channels, value_channels,
                 scales=([1]), norm_layer=nn.BatchNorm2d, concat=True, **kwargs):
        super(BaseOCModule, self).__init__()
        self.stages = nn.ModuleList([
            BaseAttentionBlock(in_channels, out_channels, key_channels, value_channels, scale, norm_layer, **kwargs)
            for scale in scales])
        in_channels = in_channels * 2 if concat else in_channels
        self.project = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 1),
            norm_layer(out_channels),
            nn.ReLU(True),
            nn.Dropout2d(0.05)
        )
        self.concat = concat

    def forward(self, x):
        priors = [stage(x) for stage in self.stages]
        context = priors[0]
        for i in range(1, len(priors)):
            context += priors[i]
        if self.concat:
            context = torch.cat([context, x], 1)
        out = self.project(context)
        return out


class PyramidAttentionBlock(nn.Module):
    """The basic implementation for pyramid self-attention block/non-local block"""

    def __init__(self, in_channels, out_channels, key_channels, value_channels,
                 scale=1, norm_layer=nn.BatchNorm2d, **kwargs):
        super(PyramidAttentionBlock, self).__init__()
        self.scale = scale
        self.value_channels = value_channels
        self.key_channels = key_channels

        self.f_value = nn.Conv2d(in_channels, value_channels, 1)
        self.f_key = nn.Sequential(
            nn.Conv2d(in_channels, key_channels, 1),
            norm_layer(key_channels),
            nn.ReLU(True)
        )
        self.f_query = self.f_key
        self.W = nn.Conv2d(value_channels, out_channels, 1)
        nn.init.constant_(self.W.weight, 0)
        nn.init.constant_(self.W.bias, 0)

    def forward(self, x):
        batch_size, c, w, h = x.size()

        local_x = list()
        local_y = list()
        step_w, step_h = w // self.scale, h // self.scale
        for i in range(self.scale):
            for j in range(self.scale):
                start_x, start_y = step_w * i, step_h * j
                end_x, end_y = min(start_x + step_w, w), min(start_y + step_h, h)
                if i == (self.scale - 1):
                    end_x = w
                if j == (self.scale - 1):
                    end_y = h
                local_x += [start_x, end_x]
                local_y += [start_y, end_y]

        value = self.f_value(x)
        query = self.f_query(x)
        key = self.f_key(x)

        local_list = list()
        local_block_cnt = (self.scale ** 2) * 2
        for i in range(0, local_block_cnt, 2):
            value_local = value[:, :, local_x[i]:local_x[i + 1], local_y[i]:local_y[i + 1]]
            query_local = query[:, :, local_x[i]:local_x[i + 1], local_y[i]:local_y[i + 1]]
            key_local = key[:, :, local_x[i]:local_x[i + 1], local_y[i]:local_y[i + 1]]

            w_local, h_local = value_local.size(2), value_local.size(3)
            value_local = value_local.contiguous().view(batch_size, self.value_channels, -1).permute(0, 2, 1)
            query_local = query_local.contiguous().view(batch_size, self.key_channels, -1).permute(0, 2, 1)
            key_local = key_local.contiguous().view(batch_size, self.key_channels, -1)

            sim_map = torch.bmm(query_local, key_local) * (self.key_channels ** -.5)
            sim_map = F.softmax(sim_map, dim=-1)

            context_local = torch.bmm(sim_map, value_local).permute(0, 2, 1).contiguous()
            context_local = context_local.view(batch_size, self.value_channels, w_local, h_local)
            local_list.append(context_local)

        context_list = list()
        for i in range(0, self.scale):
            row_tmp = list()
            for j in range(self.scale):
                row_tmp.append(local_list[j + i * self.scale])
            context_list.append(torch.cat(row_tmp, 3))

        context = torch.cat(context_list, 2)
        context = self.W(context)

        return context


class PyramidOCModule(nn.Module):
    """Pyramid-OC"""

    def __init__(self, in_channels, out_channels, key_channels, value_channels,
                 scales=([1]), norm_layer=nn.BatchNorm2d, **kwargs):
        super(PyramidOCModule, self).__init__()
        self.stages = nn.ModuleList([
            PyramidAttentionBlock(in_channels, out_channels, key_channels, value_channels, scale, norm_layer, **kwargs)
            for scale in scales])
        self.up_dr = nn.Sequential(
            nn.Conv2d(in_channels, in_channels * len(scales), 1),
            norm_layer(in_channels * len(scales)),
            nn.ReLU(True)
        )
        self.project = nn.Sequential(
            nn.Conv2d(in_channels * len(scales) * 2, out_channels, 1),
            norm_layer(out_channels),
            nn.ReLU(True),
            nn.Dropout2d(0.05)
        )

    def forward(self, x):
        priors = [stage(x) for stage in self.stages]
        context = [self.up_dr(x)]
        for i in range(len(priors)):
            context += [priors[i]]
        context = torch.cat(context, 1)
        out = self.project(context)
        return out


class ASPOCModule(nn.Module):
    """ASP-OC"""

    def __init__(self, in_channels, out_channels, key_channels, value_channels,
                 atrous_rates=(12, 24, 36), norm_layer=nn.BatchNorm2d, **kwargs):
        super(ASPOCModule, self).__init__()
        self.context = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            norm_layer(out_channels),
            nn.ReLU(True),
            BaseOCModule(out_channels, out_channels, key_channels, value_channels, ([2]), norm_layer, False, **kwargs))

        rate1, rate2, rate3 = tuple(atrous_rates)
        self.b1 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=rate1, dilation=rate1, bias=False),
            norm_layer(out_channels),
            nn.ReLU(True))
        self.b2 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=rate2, dilation=rate2, bias=False),
            norm_layer(out_channels),
            nn.ReLU(True))
        self.b3 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=rate3, dilation=rate3, bias=False),
            norm_layer(out_channels),
            nn.ReLU(True))
        self.b4 = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 1, bias=False),
            norm_layer(out_channels),
            nn.ReLU(True))

        self.project = nn.Sequential(
            nn.Conv2d(out_channels * 5, out_channels, 1, bias=False),
            norm_layer(out_channels),
            nn.ReLU(True),
            nn.Dropout2d(0.1)
        )

    def forward(self, x):
        feat1 = self.context(x)
        feat2 = self.b1(x)
        feat3 = self.b2(x)
        feat4 = self.b3(x)
        feat5 = self.b4(x)
        out = torch.cat((feat1, feat2, feat3, feat4, feat5), dim=1)
        out = self.project(out)
        return out

