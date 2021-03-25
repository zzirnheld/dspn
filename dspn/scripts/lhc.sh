#!/bin/bash

# ARGS="--show --loss chamfer --encoder FSEncoder --lr 0.01 --dim 256 --dataset mnist --epochs 100 --latent 64 --mask-feature --inner-lr 800"
ARGS="--show --loss chamfer --encoder FSEncoder --lr 0.01 --dim 256 --dataset lhc --epochs 2 --latent 64 --mask-feature --inner-lr 800"

# DSPN train
python3 train.py $ARGS --decoder DSPN --name dspn-mnist
# DSPN test and export
python3 train.py $ARGS --decoder DSPN --name test --resume logs/dspn-mnist --eval-only --export-progress --export-dir out/mnist/dspn
