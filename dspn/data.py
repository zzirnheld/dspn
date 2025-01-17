import os
import math
import random
import json

import torch
import torch.utils.data
import torchvision
import torchvision.transforms as transforms
import torchvision.transforms.functional as T
import h5py
import numpy as np

import pandas

def get_loader(dataset, batch_size, num_workers=8, shuffle=True):
    dl = torch.utils.data.DataLoader(
        dataset,
        shuffle=shuffle,
        batch_size=batch_size,
        pin_memory=True,
        num_workers=num_workers,
        drop_last=True,
    )

    print(f'datatset len: {len(dataset)}, dataloader len: {len(dl)}')

    return dl

class LHCSet(torch.utils.data.Dataset):
    def __init__(self, train=True):
        data_path = '/home/zzirnhel/Desktop/events_LHCO2020_BlackBox1.h5'
        label_path = '/home/zzirnhel/Desktop/events_LHCO2020_BlackBox1.masterkey'

        self.train = train
        self.data = self.cache(data_path, label_path)

    def cache(self, data_path, label_path):
        df_interval = 10000
        num_to_load = 10000 if self.train else 200

        label_file = open(label_path, 'r')
        labels = label_file.readlines()
        labels = [1 if l == '1.0\n' else 0 for l in labels]
        label_file.close()

        desired_label = 0 if self.train else 1
        desired_labels = []
        for i, l in enumerate(labels):
            if l == desired_label:
                desired_labels.append(i)
                if len(desired_labels) > num_to_load:
                    break
            
        if len(desired_labels) > num_to_load:
            desired_labels = desired_labels[:num_to_load]

        data = []

        rowmax = 900
        currmax = 0
        #iterate across dataframe
        for i in desired_labels:
            while currmax - 1 < i:
                print(f'loading pandas df from {currmax} to {currmax + df_interval}')
                df = pandas.read_hdf(data_path, start=currmax, stop=currmax + df_interval)
                print('loaded pandas df')
                currmax += df_interval
            
            index = i + df_interval - currmax
            row = df.iloc[index]

            #check if the number of nonzero points is greater than a threshold. if not, throw it out.
            for index in range(len(row) - 1, rowmax - 1, -1):
                if row[index] != 0:
                    #print('nonzero at', index)
                    break
            if index > rowmax:
                continue

            point_set = torch.FloatTensor(row[:rowmax]).view((3, rowmax // 3))
            #point_set = torch.FloatTensor(row[:rowmax]).unsqueeze(0)
            #print('point set shape', point_set.shape)
            label = desired_label
            _, cardinality = point_set.shape
            data.append((point_set, label, cardinality))

        print('finished with', len(data), 'sets')

        return data

    #needs to override __getitem__
    def __getitem__(self, item):
        s, l, c = self.data[item]
        mask = torch.ones(c)
        return l, s, mask

    def __len__(self):
        return len(self.data)

class MNISTSet(torch.utils.data.Dataset):
    def __init__(self, threshold=0.0, train=True, root="mnist", full=False):
        self.train = train
        self.root = root
        self.threshold = threshold
        self.full = full
        transform = transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
        )

        torchvision.datasets.MNIST.resources = [
            ('https://ossci-datasets.s3.amazonaws.com/mnist/train-images-idx3-ubyte.gz', 'f68b3c2dcbeaaa9fbdd348bbdeb94873'),
            ('https://ossci-datasets.s3.amazonaws.com/mnist/train-labels-idx1-ubyte.gz', 'd53e105ee54ea40749a09fcbcd1e9432'),
            ('https://ossci-datasets.s3.amazonaws.com/mnist/t10k-images-idx3-ubyte.gz', '9fb629c4189551a2d022fa330f9573f3'),
            ('https://ossci-datasets.s3.amazonaws.com/mnist/t10k-labels-idx1-ubyte.gz', 'ec29112dd5afa0611ce80d1b7f02629c')
        ]
        mnist = torchvision.datasets.MNIST(
            train=train, transform=transform, download=True, root=root
        )
        self.data = self.cache(mnist)
        self.max = 342

    def cache(self, dataset):
        cache_path = os.path.join(self.root, f"mnist_{self.train}_{self.threshold}.pth")
        if os.path.exists(cache_path):
            return torch.load(cache_path)

        print("Processing dataset...")
        data = []
        for datapoint in dataset:
            img, label = datapoint
            point_set, cardinality = self.image_to_set(img)
            data.append((point_set, label, cardinality))
            '''
            print('set', point_set)
            print(label)
            print(cardinality)
            raise Exception()
            '''
        torch.save(data, cache_path)
        print("Done!", len(data), "datapoints.")
        return data

    def image_to_set(self, img):
        idx = (img.squeeze(0) > self.threshold).nonzero().transpose(0, 1)
        cardinality = idx.size(1)
        return idx, cardinality

    def __getitem__(self, item):
        s, l, c = self.data[item]
        # make sure set is shuffled
        s = s[:, torch.randperm(c)]
        # pad to fixed size
        padding_size = self.max - s.size(1)
        s = torch.cat([s.float(), torch.zeros(2, padding_size)], dim=1)
        # put in range [0, 1]
        s = s / 27
        # mask of which elements are valid,not padding
        mask = torch.zeros(self.max)
        mask[:c].fill_(1)
        return l, s, mask

    def __len__(self):
        if self.train or self.full:
            return len(self.data)
        else:
            return len(self.data) // 10


CLASSES = {
    "material": ["rubber", "metal"],
    "color": ["cyan", "blue", "yellow", "purple", "red", "green", "gray", "brown"],
    "shape": ["sphere", "cube", "cylinder"],
    "size": ["large", "small"],
}


class CLEVR(torch.utils.data.Dataset):
    def __init__(self, base_path, split, box=False, full=False):
        assert split in {
            "train",
            "val",
            "test",
        }  # note: test isn't very useful since it doesn't have ground-truth scene information
        self.base_path = base_path
        self.split = split
        self.max_objects = 10
        self.box = box  # True if clevr-box version, False if clevr-state version
        self.full = full  # Use full validation set?

        with self.img_db() as db:
            ids = db["image_ids"]
            self.image_id_to_index = {id: i for i, id in enumerate(ids)}
        self.image_db = None

        with open(self.scenes_path) as fd:
            scenes = json.load(fd)["scenes"]
        self.img_ids, self.scenes = self.prepare_scenes(scenes)

    def object_to_fv(self, obj):
        coords = [p / 3 for p in obj["3d_coords"]]
        one_hot = lambda key: [obj[key] == x for x in CLASSES[key]]
        material = one_hot("material")
        color = one_hot("color")
        shape = one_hot("shape")
        size = one_hot("size")
        assert sum(material) == 1
        assert sum(color) == 1
        assert sum(shape) == 1
        assert sum(size) == 1
        # concatenate all the classes
        return coords + material + color + shape + size

    def prepare_scenes(self, scenes_json):
        img_ids = []
        scenes = []
        for scene in scenes_json:
            img_idx = scene["image_index"]
            # different objects depending on bbox version or attribute version of CLEVR sets
            if self.box:
                objects = self.extract_bounding_boxes(scene)
                objects = torch.FloatTensor(objects)
            else:
                objects = [self.object_to_fv(obj) for obj in scene["objects"]]
                objects = torch.FloatTensor(objects).transpose(0, 1)
            num_objects = objects.size(1)
            # pad with 0s
            if num_objects < self.max_objects:
                objects = torch.cat(
                    [
                        objects,
                        torch.zeros(objects.size(0), self.max_objects - num_objects),
                    ],
                    dim=1,
                )
            # fill in masks
            mask = torch.zeros(self.max_objects)
            mask[:num_objects] = 1

            img_ids.append(img_idx)
            scenes.append((objects, mask))
        return img_ids, scenes

    def extract_bounding_boxes(self, scene):
        """
        Code used for 'Object-based Reasoning in VQA' to generate bboxes
        https://arxiv.org/abs/1801.09718
        https://github.com/larchen/clevr-vqa/blob/master/bounding_box.py#L51-L107
        """
        objs = scene["objects"]
        rotation = scene["directions"]["right"]

        num_boxes = len(objs)

        boxes = np.zeros((1, num_boxes, 4))

        xmin = []
        ymin = []
        xmax = []
        ymax = []
        classes = []
        classes_text = []

        for i, obj in enumerate(objs):
            [x, y, z] = obj["pixel_coords"]

            [x1, y1, z1] = obj["3d_coords"]

            cos_theta, sin_theta, _ = rotation

            x1 = x1 * cos_theta + y1 * sin_theta
            y1 = x1 * -sin_theta + y1 * cos_theta

            height_d = 6.9 * z1 * (15 - y1) / 2.0
            height_u = height_d
            width_l = height_d
            width_r = height_d

            if obj["shape"] == "cylinder":
                d = 9.4 + y1
                h = 6.4
                s = z1

                height_u *= (s * (h / d + 1)) / ((s * (h / d + 1)) - (s * (h - s) / d))
                height_d = height_u * (h - s + d) / (h + s + d)

                width_l *= 11 / (10 + y1)
                width_r = width_l

            if obj["shape"] == "cube":
                height_u *= 1.3 * 10 / (10 + y1)
                height_d = height_u
                width_l = height_u
                width_r = height_u

            obj_name = (
                obj["size"]
                + " "
                + obj["color"]
                + " "
                + obj["material"]
                + " "
                + obj["shape"]
            )
            ymin.append((y - height_d) / 320.0)
            ymax.append((y + height_u) / 320.0)
            xmin.append((x - width_l) / 480.0)
            xmax.append((x + width_r) / 480.0)

        return xmin, ymin, xmax, ymax

    @property
    def images_folder(self):
        return os.path.join(self.base_path, "images", self.split)

    @property
    def scenes_path(self):
        if self.split == "test":
            raise ValueError("Scenes are not available for test")
        return os.path.join(
            self.base_path, "scenes", "CLEVR_{}_scenes.json".format(self.split)
        )

    def img_db(self):
        path = os.path.join(self.base_path, "{}-images.h5".format(self.split))
        return h5py.File(path, "r")

    def load_image(self, image_id):
        if self.image_db is None:
            self.image_db = self.img_db()
        index = self.image_id_to_index[image_id]
        image = self.image_db["images"][index]
        return image

    def __getitem__(self, item):
        image_id = self.img_ids[item]
        image = self.load_image(image_id)
        objects, size = self.scenes[item]
        return image, objects, size

    def __len__(self):
        if self.split == "train" or self.full:
            return len(self.scenes)
        else:
            return len(self.scenes) // 10


if __name__ == "__main__":
    import matplotlib.pyplot as plt

    dataset = Circles()
    for i in range(2):
        points, centre, n_points = dataset[i]
        x, y = points[0], points[1]
        plt.scatter(x.numpy(), y.numpy())
        plt.scatter(centre[0], centre[1])
    plt.axes().set_aspect("equal", "datalim")
    plt.show()
