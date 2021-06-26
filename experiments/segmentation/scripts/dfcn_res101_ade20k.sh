# !/usr/bin/env bash
# train
python -m experiments.segmentation.train --dataset ade20k \
    --model dfcn --aux --base-size 520 --crop-size 520 \
    --backbone resnet101 --checkname dfcn_res101_ade20k --dilated

#test [single-scale]
python -m experiments.segmentation.test_whole --dataset ade20k \
    --model dfcn --aux --base-size 520 --crop-size 520 \
    --backbone resnet101 --resume experiments/segmentation/runs/ade20k/dfcn/dfcn_res101_ade20k/model_best.pth.tar --split val --mode testval --dilated

#test [multi-scale]
python -m experiments.segmentation.test_whole --dataset ade20k \
    --model dfcn --aux --base-size 520 --crop-size 520 \
    --backbone resnet101 --resume experiments/segmentation/runs/ade20k/dfcn/dfcn_res101_ade20k/model_best.pth.tar --split val --mode testval --ms --dilated