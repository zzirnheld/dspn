import os
import argparse
from datetime import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F

import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torch.multiprocessing as mp

import scipy.optimize
import numpy as np
from tqdm import tqdm
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from tensorboardX import SummaryWriter

import data
import track
import model
import utils
import math
import pickle

import dummymlp

from sklearn.manifold import TSNE

def main():
    global net
    global test_loader
    global scatter
    parser = argparse.ArgumentParser()
    # generic params
    parser.add_argument(
        "--name",
        default=datetime.now().strftime("%Y-%m-%d_%H:%M:%S"),
        help="Name to store the log file as",
    )
    parser.add_argument("--resume", help="Path to log file to resume from")

    parser.add_argument("--encoder", default="FSEncoder", help="Encoder")
    parser.add_argument("--decoder", default="DSPN", help="Decoder")
    parser.add_argument(
        "--epochs", type=int, default=10, help="Number of epochs to train with"
    )
    parser.add_argument(
        "--latent", type=int, default=32, help="Dimensionality of latent space"
    )
    parser.add_argument(
        "--dim", type=int, default=64, help="Dimensionality of hidden layers"
    )
    parser.add_argument(
        "--lr", type=float, default=1e-2, help="Outer learning rate of model"
    )
    parser.add_argument(
        "--batch-size", type=int, default=32, help="Batch size to train with"
    )
    parser.add_argument(
        "--num-workers", type=int, default=2, help="Number of threads for data loader"
    )
    parser.add_argument(
        "--dataset",
        choices=["mnist", "clevr-box", "clevr-state", "lhc"],
        help="Use MNIST dataset",
    )
    parser.add_argument(
        "--no-cuda",
        action="store_true",
        help="Run on CPU instead of GPU (not recommended)",
    )
    parser.add_argument(
        "--train-only", action="store_true", help="Only run training, no evaluation"
    )
    parser.add_argument(
        "--eval-only", action="store_true", help="Only run evaluation, no training"
    )
    parser.add_argument("--multi-gpu", action="store_true", help="Use multiple GPUs")
    parser.add_argument(
        "--show", action="store_true", help="Plot generated samples in Tensorboard"
    )

    parser.add_argument("--supervised", action="store_true", help="")
    parser.add_argument("--baseline", action="store_true", help="Use baseline model")

    parser.add_argument("--export-dir", type=str, help="Directory to output samples to")
    parser.add_argument(
        "--export-n", type=int, default=10 ** 9, help="How many samples to output"
    )
    parser.add_argument(
        "--export-progress",
        action="store_true",
        help="Output intermediate set predictions for DSPN?",
    )
    parser.add_argument(
        "--full-eval",
        action="store_true",
        help="Use full evaluation set (default: 1/10 of evaluation data)",  # don't need full evaluation when training to save some time
    )
    parser.add_argument(
        "--mask-feature",
        action="store_true",
        help="Treat mask as a feature to compute loss with",
    )
    parser.add_argument(
        "--inner-lr",
        type=float,
        default=800,
        help="Learning rate of DSPN inner optimisation",
    )
    parser.add_argument(
        "--iters",
        type=int,
        default=10,
        help="How many DSPN inner optimisation iteration to take",
    )
    parser.add_argument(
        "--huber-repr",
        type=float,
        default=1,
        help="Scaling of representation loss term for DSPN supervised learning",
    )
    parser.add_argument(
        "--loss",
        choices=["hungarian", "chamfer"],
        default="hungarian",
        help="Type of loss used",
    )
    args = parser.parse_args()

    train_writer = SummaryWriter(f"runs/{args.name}", purge_step=0)

    net = model.build_net(args)

    if not args.no_cuda:
        net = net.cuda()

    if args.multi_gpu:
        net = torch.nn.DataParallel(net)

    optimizer = torch.optim.Adam(
        [p for p in net.parameters() if p.requires_grad], lr=args.lr
    )

    if args.dataset == "mnist":
        dataset_train = data.MNISTSet(train=True, full=args.full_eval)
        dataset_test = data.MNISTSet(train=False, full=args.full_eval)
    elif args.dataset == "lhc":
        dataset_train = data.LHCSet(train=True)
        dataset_test = data.LHCSet(train=False)
    else:
        dataset_train = data.CLEVR(
            "clevr", "train", box=args.dataset == "clevr-box", full=args.full_eval
        )
        dataset_test = data.CLEVR(
            "clevr", "val", box=args.dataset == "clevr-box", full=args.full_eval
        )

    if not args.eval_only:
        train_loader = data.get_loader(
            dataset_train, batch_size=args.batch_size, num_workers=args.num_workers
        )
    if not args.train_only:
        test_loader = data.get_loader(
            dataset_test,
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            shuffle=False,
        )

    tracker = track.Tracker(
        train_mae=track.ExpMean(),
        train_last=track.ExpMean(),
        train_loss=track.ExpMean(),
        test_mae=track.Mean(),
        test_last=track.Mean(),
        test_loss=track.Mean(),
    )

    if args.resume:
        log = torch.load(args.resume)
        weights = log["weights"]
        n = net
        if args.multi_gpu:
            n = n.module
        n.load_state_dict(weights, strict=True)

    def run(net, loader, optimizer, train=False, epoch=0, pool=None):
        writer = train_writer
        if train:
            net.train()
            prefix = "train"
            torch.set_grad_enabled(True)
        else:
            net.eval()
            prefix = "test"
            torch.set_grad_enabled(False)

        total_train_steps = args.epochs * len(loader)
        if args.export_dir:
            true_export = []
            pred_export = []

        iters_per_epoch = len(loader) #len(loader) is # batches, which is len(dataset)/batch size (which is an arg)
        loader = tqdm(
            loader,
            ncols=0,
            desc="{1} E{0:02d}".format(epoch, "train" if train else "test "),
        )

        losses = []
        steps = []
        encodings = []
        for i, sample in enumerate(loader, start=epoch * iters_per_epoch):
            # input is either a set or an image
            input, target_set, target_mask = map(lambda x: x.cuda(), sample)

            # forward evaluation through the network
            (progress, masks, evals, gradn), (y_enc, y_label) = net(
                input, target_set, target_mask
            )

            encodings.append(y_label)
            progress_only = progress

            # if using mask as feature, concat mask feature into progress
            if args.mask_feature:
                target_set = torch.cat(
                    [target_set, target_mask.unsqueeze(dim=1)], dim=1
                )
                progress = [
                    torch.cat([p, m.unsqueeze(dim=1)], dim=1)
                    for p, m in zip(progress, masks)
                ]

            if args.loss == "chamfer":
                # dim 0 is over the inner iteration steps
                # target set is broadcasted over dim 0
                set_loss = utils.chamfer_loss(
                    torch.stack(progress), target_set.unsqueeze(0)
                )
            else:
                # dim 0 is over the inner iteration steps
                a = torch.stack(progress)
                # target set is explicitly broadcasted over dim 0
                b = target_set.repeat(a.size(0), 1, 1, 1)
                # flatten inner iteration dim and batch dim
                a = a.view(-1, a.size(2), a.size(3))
                b = b.view(-1, b.size(2), b.size(3))
                set_loss = utils.hungarian_loss(
                    progress[-1], target_set, thread_pool=pool
                ).unsqueeze(0)
            # Only use representation loss with DSPN and when doing general supervised prediction, not when auto-encoding
            if args.supervised and not args.baseline:
                repr_loss = args.huber_repr * F.smooth_l1_loss(y_enc, y_label)
                loss = set_loss.mean() + repr_loss.mean()
            else:
                loss = set_loss.mean()

            # restore progress variable to not contain masks for correct exporting
            progress = progress_only

            # Outer optim step
            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            # loss value can be gotten as loss.item() for graphing purposes
            steps.append(i)
            losses.append(loss.item())
            # Tensorboard tracking of metrics for debugging
            tracked_last = tracker.update("{}_last".format(prefix), set_loss[-1].item())
            tracked_loss = tracker.update("{}_loss".format(prefix), loss.item())
            if train:
                writer.add_scalar("metric/set-loss", loss.item(), global_step=i)
                writer.add_scalar(
                    "metric/set-last", set_loss[-1].mean().item(), global_step=i
                )
                if not args.baseline:
                    writer.add_scalar(
                        "metric/eval-first", evals[0].mean().item(), global_step=i
                    )
                    writer.add_scalar(
                        "metric/eval-last", evals[-1].mean().item(), global_step=i
                    )
                    writer.add_scalar(
                        "metric/max-inner-grad-norm",
                        max(g.item() for g in gradn),
                        global_step=i,
                    )
                    writer.add_scalar(
                        "metric/mean-inner-grad-norm",
                        sum(g.item() for g in gradn) / len(gradn),
                        global_step=i,
                    )
                    if args.supervised:
                        writer.add_scalar(
                            "metric/repr_loss", repr_loss.item(), global_step=i
                        )

            # Print current progress to progress bar
            fmt = "{:.6f}".format
            loader.set_postfix(
                last=fmt(tracked_last),
                loss=fmt(tracked_loss),
                bad=fmt(evals[-1].detach().cpu().item() * 1000)
                if not args.baseline
                else 0,
            )

            # Store predictions to be exported
            if args.export_dir:
                if len(true_export) < args.export_n:
                    for p, m in zip(target_set, target_mask):
                        true_export.append(p.detach().cpu())
                    progress_steps = []
                    for pro, mas in zip(progress, masks):
                        # pro and mas are one step of the inner optim
                        # score boxes contains the list of predicted elements for one step
                        score_boxes = []
                        for p, m in zip(pro.cpu().detach(), mas.cpu().detach()):
                            score_box = torch.cat([m.unsqueeze(0), p], dim=0)
                            score_boxes.append(score_box)
                        progress_steps.append(score_boxes)
                    for b in zip(*progress_steps):
                        pred_export.append(b)

            # Plot predictions in Tensorboard
            if args.show and not train:
                name = f"set/epoch-{epoch}/img-{i}"
                # thresholded set
                progress.append(progress[-1])
                masks.append((masks[-1] > 0.5).float())
                # target set
                if args.mask_feature:
                    # target set is augmented with masks, so remove them
                    progress.append(target_set[:, :-1])
                else:
                    progress.append(target_set)
                masks.append(target_mask)
                # intermediate sets
                for j, (s, ms) in enumerate(zip(progress, masks)):
                    if args.dataset == "clevr-state":
                        continue
                    s, ms = utils.scatter_masked(
                        s,
                        ms,
                        binned=args.dataset.startswith("clevr"),
                        threshold=0.5 if args.dataset.startswith("clevr") else None,
                    )
                    tag_name = f"{name}" if j != len(progress) - 1 else f"{name}-target"
                    if args.dataset == "clevr-box":
                        img = input[0].detach().cpu()
                        writer.add_image_with_boxes(
                            tag_name, img, s.transpose(0, 1), global_step=j
                        )
                    elif args.dataset == "clevr-state":
                        pass
                    else:  # mnist
                        fig = plt.figure()
                        y, x = s
                        y = 1 - y
                        ms = ms.numpy()
                        rgba_colors = np.zeros((ms.size, 4))
                        rgba_colors[:, 2] = 1.0
                        rgba_colors[:, 3] = ms
                        plt.scatter(x, y, color=rgba_colors)
                        plt.axes().set_aspect("equal")
                        plt.xlim(0, 1)
                        plt.ylim(0, 1)
                        writer.add_figure(tag_name, fig, global_step=j)




        #create scatter of encoded points?
        print([t.shape for t in encodings[0:5]])
        '''
        #get the first latent dimension, and plot that
        first_latent_dim = [t.detach().cpu()[0] for t in encodings[100:]]
        numbers = list(range(len(first_latent_dim[0]))) * len(first_latent_dim)
        first_latent_dim_as_list = []
        for tensor in first_latent_dim:
            for entry in tensor:
                first_latent_dim_as_list.append(entry)
        
        print(len(numbers))
        print(len(first_latent_dim_as_list))
        fig = plt.figure()
        #plt.yscale('log')
        #print(first_latent_dim[0:5])
        plt.scatter(numbers, first_latent_dim_as_list)
        name = f'img-latent-epoch-{epoch}-{"train" if train else "test"}'
        plt.savefig(name, dpi=300)
        plt.close(fig)
        '''

        #create scatter of encoded points, put through tsne
        # see: https://scikit-learn.org/stable/modules/generated/sklearn.manifold.TSNE.html
        set_encoding_in_hidden_dim = torch.cat(encodings, dim = 0).detach().cpu()
        set_encoding_tsne = TSNE(n_components = 2).fit_transform(set_encoding_in_hidden_dim)
        set_encoding_tsne_x = set_encoding_tsne[:, 0]
        set_encoding_tsne_y = set_encoding_tsne[:, 1]
        fig = plt.figure()
        plt.scatter(set_encoding_tsne_x, set_encoding_tsne_y)
        name = f'img-latent-epoch-{epoch}-{"train" if train else "test"}'
        plt.savefig(name, dpi=300)
        plt.close(fig)

        # create graph
        fig = plt.figure()
        #plt.yscale('linear')
        plt.scatter(steps, losses)
        name = f"img-losses-epoch-{epoch}-train-{'train' if train else 'test'}"
        plt.savefig(name, dpi=300)
        plt.close(fig)

        # Export predictions
        if args.export_dir:
            os.makedirs(f"{args.export_dir}/groundtruths", exist_ok=True)
            os.makedirs(f"{args.export_dir}/detections", exist_ok=True)
            for i, (gt, dets) in enumerate(zip(true_export, pred_export)):
                with open(f"{args.export_dir}/groundtruths/{i}.txt", "w") as fd:
                    for box in gt.transpose(0, 1):
                        if (box == 0).all():
                            continue
                        s = "box " + " ".join(map(str, box.tolist()))
                        fd.write(s + "\n")
                if args.export_progress:
                    for step, det in enumerate(dets):
                        with open(
                            f"{args.export_dir}/detections/{i}-step{step}.txt", "w"
                        ) as fd:
                            for sbox in det.transpose(0, 1):
                                s = f"box " + " ".join(map(str, sbox.tolist()))
                                fd.write(s + "\n")
                with open(f"{args.export_dir}/detections/{i}.txt", "w") as fd:
                    for sbox in dets[-1].transpose(0, 1):
                        s = f"box " + " ".join(map(str, sbox.tolist()))
                        fd.write(s + "\n")

        return np.mean(losses)

    import subprocess

    git_hash = subprocess.check_output(["git", "rev-parse", "HEAD"])

    torch.backends.cudnn.benchmark = True

    epoch_losses = []

    for epoch in range(args.epochs):
        tracker.new_epoch()
        with mp.Pool(10) as pool:
            if not args.eval_only:
                e_loss = run(net, train_loader, optimizer, train=True, epoch=epoch, pool=pool)
                epoch_losses.append(e_loss)
            if not args.train_only:
                e_loss = run(net, test_loader, optimizer, train=False, epoch=epoch, pool=pool)

        results = {
            "name": args.name,
            "tracker": tracker.data,
            "weights": net.state_dict()
            if not args.multi_gpu
            else net.module.state_dict(),
            "args": vars(args),
            "hash": git_hash,
        }
        torch.save(results, os.path.join("logs", args.name))
        if args.eval_only:
            break

    #plot encoding/decoding
    #train loader is bkdg points, test loader is signal points
    with mp.Pool(10) as pool:
        train_encodings = []
        for i, sample in enumerate(train_loader):
            # input is either a set or an image
            input, target_set, target_mask = map(lambda x: x.cuda(), sample)

            # forward evaluation through the network
            (progress, masks, evals, gradn), (y_enc, y_label) = net(
                input, target_set, target_mask
            )
            train_encodings.append(y_label)
        test_encodings = []
        for i, sample in enumerate(test_loader):
            # input is either a set or an image
            input, target_set, target_mask = map(lambda x: x.cuda(), sample)

            # forward evaluation through the network
            (progress, masks, evals, gradn), (y_enc, y_label) = net(
                input, target_set, target_mask
            )
            test_encodings.append(y_label)



    #recall: "train" is actually just bkgd and "test" is really just signal,
    #because we're trying to learn the background distribution and then encode signal points in the same way, and see what happens
    #so train an MLP and see if it can distinguish
    num_bkdg_val = len(train_encodings) // 3
    num_sign_val = len(test_encodings) // 3
    bkgd_train_encodings_tensor = torch.cat(train_encodings[:-num_bkdg_val], dim = 0)
    sign_train_encodings_tensor = torch.cat(test_encodings[:-num_sign_val], dim = 0)
    bkgd_train_encoding_label_tensor = torch.zeros((len(train_encodings) - num_bkdg_val) * args.batch_size, dtype=torch.long)
    sign_train_encoding_label_tensor = torch.ones((len(test_encodings) - num_sign_val) * args.batch_size, dtype=torch.long)

    train_encodings_tensor = torch.cat((bkgd_train_encodings_tensor, sign_train_encodings_tensor)).to('cuda:0')
    train_encodings_tensor_copy = torch.tensor(train_encodings_tensor, requires_grad=True).to('cuda:0')
    train_labels_tensor = torch.cat((bkgd_train_encoding_label_tensor, sign_train_encoding_label_tensor)).to('cuda:0')

    bkgd_test_encodings_tensor = torch.cat(train_encodings[-num_bkdg_val:])
    sign_test_encodings_tensor = torch.cat(test_encodings[-num_sign_val:])
    bkgd_test_encoding_label_tensor = torch.zeros((num_bkdg_val) * args.batch_size, dtype=torch.long)
    sign_test_encoding_label_tensor = torch.ones((num_sign_val) * args.batch_size, dtype=torch.long)

    test_encodings_tensor = torch.cat((bkgd_test_encodings_tensor, sign_test_encodings_tensor)).to('cuda:0')
    test_encodings_tensor_copy = torch.tensor(test_encodings_tensor, requires_grad=True).to('cuda:0')
    test_labels_tensor = torch.cat((bkgd_test_encoding_label_tensor, sign_test_encoding_label_tensor)).to('cuda:0')

    _, encoding_len = y_label.shape
    mlp = dummymlp.MLP(embedding_dim=encoding_len, hidden_dim=20, label_dim=2).to('cuda:0')
    loss_func = nn.CrossEntropyLoss().to('cuda:0')
    #mlp.train()

    epochs = 20
    for epoch in range(epochs):
        mlp.zero_grad()
        output = mlp(train_encodings_tensor_copy)
        loss = loss_func(output, train_labels_tensor)
        loss.backward()

    #test
    #mlp.eval()
    test_output = mlp(test_encodings_tensor_copy)
    argmaxed_test_output = torch.argmax(test_output, dim=1)
    total = argmaxed_test_output.size(0)
    correct = (argmaxed_test_output == test_labels_tensor).sum().item()

    print(f'acc = {correct / total}')



    #create scatter of encoded points, put through tsne
    # see: https://scikit-learn.org/stable/modules/generated/sklearn.manifold.TSNE.html
    tsne = TSNE(n_components = 2)
    num_test = len(test_encodings) * args.batch_size

    fig = plt.figure()
    set_encoding_in_hidden_dim_train = torch.cat(train_encodings, dim = 0).detach().cpu()
    set_encoding_in_hidden_dim_test = torch.cat(test_encodings, dim = 0).detach().cpu()
    set_encoding_in_hidden_dim = torch.cat((set_encoding_in_hidden_dim_test, set_encoding_in_hidden_dim_train), dim = 0)

    set_encoding_tsne = tsne.fit_transform(set_encoding_in_hidden_dim)
    set_encoding_tsne_x_train = set_encoding_tsne[num_test:, 0]
    set_encoding_tsne_y_train = set_encoding_tsne[num_test:, 1]
    set_encoding_tsne_x_test = set_encoding_tsne[:num_test, 0]
    set_encoding_tsne_y_test = set_encoding_tsne[:num_test, 1]

    plt.scatter(set_encoding_tsne_x_train, set_encoding_tsne_y_train)
    plt.scatter(set_encoding_tsne_x_test, set_encoding_tsne_y_test)
    name = f'img-latent-NEW'
    plt.savefig(name, dpi=300)
    plt.close(fig)

    fig = plt.figure()
    plt.scatter(range(args.epochs), epoch_losses)
    name = f"img-losses-per-epoch-train-{'train' if not args.eval_only else 'test'}"
    plt.savefig(name, dpi=300)


if __name__ == "__main__":
    main()
