import torch.nn as nn
import torch.utils.model_zoo as model_zoo
import torch
import math
from copy import deepcopy
from custom_layers import center_crop


class OSVOS(nn.Module):

    def __init__(self, pretrained=1):
        super(OSVOS, self).__init__()
        lay_list = [[64, 64],
                    ['M', 128, 128],
                    ['M', 256, 256, 256],
                    ['M', 512, 512, 512],
                    ['M', 512, 512, 512]]
        in_channels = [3, 64, 128, 256, 512]

        stages = []
        side_prep = []
        score_dsn = []
        upscale = []

        # Attention, side_prep and score_dsn start from layer 2
        for i in range(0, len(lay_list)):
            # Make the layers of the stages
            stages.append(make_layers_osvos(lay_list[i], in_channels[i]))

            if i > 0:
                # Make the layers of the preparation step
                side_prep.append(nn.Sequential(*[nn.Conv2d(lay_list[i][-1], 16, kernel_size=3, padding=1),
                                                 nn.ReLU(inplace=True)]))

                # Make the layers of the score_dsn step
                score_dsn.append(nn.Sequential(*[nn.Conv2d(16, 1, kernel_size=1, padding=0),
                                                 nn.ReLU(inplace=True)]))

                upscale.append(nn.Upsample(scale_factor=2**i, mode='bilinear'))

        self.upscale = upscale
        self.stages = ListModule(*stages)
        self.side_prep = ListModule(*side_prep)
        self.score_dsn = ListModule(*score_dsn)

        self.fuse = nn.Conv2d(64, 1, kernel_size=1, padding=0)

        self._initialize_weights(pretrained)

    def forward(self, x):
        crop_h, crop_w = int(x.size()[2]), int(x.size()[3])
        x = self.stages[0](x)

        side = []
        side_out = []
        for i in range(1, len(self.stages)):
            x = self.stages[i](x)
            side_temp = self.upscale[i - 1](self.side_prep[i-1](x))
            side.append(center_crop(side_temp, crop_h, crop_w))
            side_out.append(center_crop(self.score_dsn[i - 1](side_temp), crop_h, crop_w))

        out = torch.cat(side[:], dim=1)
        out = self.fuse(out)
        return side_out + [out]

    def _initialize_weights(self, pretrained):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, 0.001)  # math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                n = m.weight.size(1)
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()

        if pretrained:
            _vgg = VGG(make_layers([64, 64, 'M', 128, 128, 'M', 256, 256, 256,
                                    'M', 512, 512, 512, 'M', 512, 512, 512, 'M']))

            # Load the weights from saved model
            _vgg.load_state_dict(torch.load('models/vgg16-397923af.pth',
                                 map_location=lambda storage, loc: storage))

            # Load the weights directly from the web
            # _vgg.load_state_dict(model_zoo.load_url('https://download.pytorch.org/models/vgg16-397923af.pth'))

            inds = find_conv_layers(_vgg)
            k = 0
            for i in range(len(self.stages)):
                for j in range(len(self.stages[i])):
                    if isinstance(self.stages[i][j], nn.Conv2d):
                        self.stages[i][j].weight = deepcopy(_vgg.features[inds[k]].weight)
                        self.stages[i][j].bias = deepcopy(_vgg.features[inds[k]].bias)
                        k += 1


def find_conv_layers(_vgg):
    inds = []
    for i in range(len(_vgg.features)):
        if isinstance(_vgg.features[i], nn.Conv2d):
            inds.append(i)
    return inds


def make_layers_osvos(cfg, in_channels):
    layers = []
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        else:
            conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
            layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    return nn.Sequential(*layers)


class ListModule(nn.Module):
    """ Allows the creation of lists of layers,
        which is not supported by PyTorch in general.
        Detailed discussion here: https://discuss.pytorch.org/t/list-of-nn-module-in-a-nn-module/219
    """
    def __init__(self, *args):
        super(ListModule, self).__init__()
        idx = 0
        for module in args:
            self.add_module(str(idx), module)
            idx += 1

    def __getitem__(self, idx):
        if idx < 0 or idx >= len(self._modules):
            raise IndexError('index {} is out of range'.format(idx))
        it = iter(self._modules.values())
        for i in range(idx):
            next(it)
        return next(it)

    def __iter__(self):
        return iter(self._modules.values())

    def __len__(self):
        return len(self._modules)


class VGG(nn.Module):

    def __init__(self, features, num_classes=1000):
        super(VGG, self).__init__()
        self.features = features
        self.classifier = nn.Sequential(
            nn.Linear(512 * 7 * 7, 4096),
            nn.ReLU(True),
            nn.Dropout(),
            nn.Linear(4096, 4096),
            nn.ReLU(True),
            nn.Dropout(),
            nn.Linear(4096, num_classes),
        )
        self._initialize_weights()

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                n = m.weight.size(1)
                m.weight.data.normal_(0, 0.01)
                m.bias.data.zero_()


def make_layers(cfg, batch_norm=False):
    layers = []
    in_channels = 3
    for v in cfg:
        if v == 'M':
            layers += [nn.MaxPool2d(kernel_size=2, stride=2)]
        else:
            conv2d = nn.Conv2d(in_channels, v, kernel_size=3, padding=1)
            if batch_norm:
                layers += [conv2d, nn.BatchNorm2d(v), nn.ReLU(inplace=True)]
            else:
                layers += [conv2d, nn.ReLU(inplace=True)]
            in_channels = v
    return nn.Sequential(*layers)
