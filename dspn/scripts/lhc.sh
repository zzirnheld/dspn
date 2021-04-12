#!/bin/bash

# ARGS="--show --loss chamfer --encoder FSEncoder --lr 0.01 --dim 256 --dataset mnist --epochs 100 --latent 64 --mask-feature --inner-lr 800"
ARGS="--loss hungarian --encoder FSEncoder --lr 0.01 --dim 256 --dataset lhc --epochs 5 --batch-size 16 --latent 64 --mask-feature --inner-lr 800"

# DSPN train
python3 train.py $ARGS --decoder DSPN --name dspn-lhc
# DSPN test and export
python3 train.py $ARGS --decoder DSPN --name test --resume logs/dspn-lhc --eval-only --export-dir out/lhc/dspn
