# -*-coding:utf-8-*-

import torch
import torch.nn as nn
import numpy as np
from utils.utils import bboxes_iou


class YOLOLayer(nn.Module):
    """
    detection layer corresponding to yolo_layer.c of darknet
    """
    def __init__(self, config_model, layer_no, in_ch, ignore_thre=0.7):
        """
        Args:
            config_model (dict) : model configuration.
                ANCHORS (list of tuples) :
                ANCH_MASK:  (list of int list): index indicating the anchors to be
                    used in YOLO layers. One of the mask group is picked from the list.
                N_CLASSES (int): number of classes
            layer_no (int): YOLO layer number - one from (0, 1, 2).
            in_ch (int): number of input channels.
            ignore_thre (float): threshold of IoU above which objectness training is ignored.
        """

        super(YOLOLayer, self).__init__()
        strides = [32, 16, 8] # fixed
        self.anchors = config_model['ANCHORS']
        self.anch_mask = config_model['ANCH_MASK'][layer_no]
        self.n_anchors = len(self.anch_mask)
        self.n_classes = config_model['N_CLASSES']
        self.ignore_thre = ignore_thre
        self.l2_loss = nn.MSELoss(size_average=False)
        self.bce_loss = nn.BCELoss(size_average=False)
        self.stride = strides[layer_no]
        self.all_anchors_grid = [(w / self.stride, h / self.stride)
                                 for w, h in self.anchors]
        self.masked_anchors = [self.all_anchors_grid[i]
                               for i in self.anch_mask]
        self.ref_anchors = np.zeros((len(self.all_anchors_grid), 4))#这个是什么，为什么是4列
        self.ref_anchors[:, 2:] = np.array(self.all_anchors_grid)
        self.ref_anchors = torch.FloatTensor(self.ref_anchors)
        self.conv = nn.Conv2d(in_channels=in_ch,
                              out_channels=self.n_anchors * (self.n_classes + 5),
                              kernel_size=1, stride=1, padding=0)

    def forward(self, xin, labels=None):
        """
        In this
        Args:
            xin (torch.Tensor): input feature map whose size is :math:`(N, C, H, W)`, \
                where N, C, H, W denote batchsize, channel width, height, width respectively.
            labels (torch.Tensor): label data whose size is :math:`(N, K, 5)`. \
                N and K denote batchsize and number of labels.
                Each label consists of [class, xc, yc, w, h]:
                    class (float): class index.
                    xc, yc (float) : center of bbox whose values range from 0 to 1.
                    w, h (float) : size of bbox whose values range from 0 to 1.
        Returns:
            loss (torch.Tensor): total loss - the target of backprop.
            loss_xy (torch.Tensor): x, y loss - calculated by binary cross entropy (BCE) \
                with boxsize-dependent weights.
            loss_wh (torch.Tensor): w, h loss - calculated by l2 without size averaging and \
                with boxsize-dependent weights.
            loss_obj (torch.Tensor): objectness loss - calculated by BCE.
            loss_cls (torch.Tensor): classification loss - calculated by BCE for each class.
            loss_l2 (torch.Tensor): total l2 loss - only for logging.
        坐标：
        b_x = sigmoid(t_x) + c_x; b_y = sigmoid(t_y) + c_y
        b_w = a_w*e^{t_w}; b_h = a_h*e^{t_h}
        c_x, c_y是对应格子的左上角点相对于图的坐标，图左上角为原点；
        t_x, t_y 是预测的中心点偏移量；
        t_w, t_h 是预测的anchor宽和高的放缩值
        a_w, a_h是原本anchor的宽和高
        """
        output = self.conv(xin)
        # after backbone output.shape [b,(5+80)*3,size,size]
        batchsize = output.shape[0]
        # 当前特征图尺寸
        fsize = output.shape[2]
        # 85
        n_ch = 5 + self.n_classes
        dtype = torch.cuda.FloatTensor if xin.is_cuda else torch.FloatTensor

        # self.n_anchors = 3, n_ch = 5 + 80 represent[x,y,w,h,score,cls1,cls2....,cls80]
        output = output.view(batchsize, self.n_anchors, n_ch, fsize, fsize)
        # -->(batchsize, self.n_anchors, fsize, fsize, n_ch)
        output = output.permute(0, 1, 3, 4, 2)  # .contiguous()

        # logistic activation for xy, obj, cls
        # np.r_[:2, 4:n_ch]-->np.array[0,1,4,5,...,79]
        # use sigmoid for t_x,t_y and n_classes
        output[..., np.r_[:2, 4:n_ch]] = torch.sigmoid(
            output[..., np.r_[:2, 4:n_ch]])

        # calculate pred - xywh obj cls

        # generate c_x and c_y
        x_shift = dtype(np.broadcast_to(
            np.arange(fsize, dtype=np.float32), output.shape[:4]))
        y_shift = dtype(np.broadcast_to(
            np.arange(fsize, dtype=np.float32).reshape(fsize, 1), output.shape[:4]))

        # the 3 anchors use in this layer [[w_1, h_1], [w_2, h_2], [w_3, h_3]]
        masked_anchors = np.array(self.masked_anchors)

        # generate a_w and a_h
        # masked_anchors.shape[3,2], output.shape[b,3,fsize,fsize,85]
        w_anchors = dtype(np.broadcast_to(np.reshape(
            masked_anchors[:, 0], (1, self.n_anchors, 1, 1)), output.shape[:4]))
        h_anchors = dtype(np.broadcast_to(np.reshape(
            masked_anchors[:, 1], (1, self.n_anchors, 1, 1)), output.shape[:4]))
        # w_anchors.shape[b,3,fsize,fize]

        pred = output.clone()
        # b_x = sigmoid(t_x) + c_x
        pred[..., 0] += x_shift
        # b_y = sigmoid(t_y) + c_y
        pred[..., 1] += y_shift
        # b_w = a_w*e^{t_w}
        pred[..., 2] = torch.exp(pred[..., 2]) * w_anchors
        # b_h = a_h*e^{t_h}
        pred[..., 3] = torch.exp(pred[..., 3]) * h_anchors

        if labels is None:  # not training
            pred[..., :4] *= self.stride
            return pred.contiguous().view(batchsize, -1, n_ch).data

        pred = pred[..., :4].data

        # target assignment

        tgt_mask = torch.zeros(batchsize, self.n_anchors,
                               fsize, fsize, 4 + self.n_classes).type(dtype)
        obj_mask = torch.ones(batchsize, self.n_anchors,
                              fsize, fsize).type(dtype)
        tgt_scale = torch.zeros(batchsize, self.n_anchors,
                                fsize, fsize, 2).type(dtype)

        target = torch.zeros(batchsize, self.n_anchors,
                             fsize, fsize, n_ch).type(dtype)
        # labels.shape[b,50,5] 5:[cls,x,y,w,h]
        labels = labels.cpu().data
        # check data and calculate the number of objects in one image; nlabel.shape:[b,1]
        nlabel = (labels.sum(dim=2) > 0).sum(dim=1)  # number of objects

        # shape[b,50]
        truth_x_all = labels[:, :, 1] * fsize
        truth_y_all = labels[:, :, 2] * fsize
        truth_w_all = labels[:, :, 3] * fsize
        truth_h_all = labels[:, :, 4] * fsize
        # TODO: is all Integer?
        # !yes
        # 真实框左上角左边
        truth_i_all = truth_x_all.to(torch.int16).numpy()
        truth_j_all = truth_y_all.to(torch.int16).numpy()

        for b in range(batchsize):
            # 取出该张图片内物体（目标）的数量
            n = int(nlabel[b])
            # if equal 0 ,meaning no object
            if n == 0:
                continue
            truth_box = dtype(np.zeros((n, 4)))
            # 把GT框的w, h 存入truth_box
            truth_box[:n, 2] = truth_w_all[b, :n]
            truth_box[:n, 3] = truth_h_all[b, :n]
            # 真实框左上角坐标
            truth_i = truth_i_all[b, :n]
            truth_j = truth_j_all[b, :n]

            # calculate iou between truth and reference anchors shape;[num_of_truthbox, all_anchor(9)]
            # self.ref_anchors 指所有9个anchor，经过下采样变换之后
            anchor_ious_all = bboxes_iou(truth_box.cpu(), self.ref_anchors)
            # get index of max iou in all_anchor，按行获得最大iou的索引，即在9个anchor框中找到与该truth_box的iou最大的一个
            best_n_all = np.argmax(anchor_ious_all, axis=1)
            best_n = best_n_all % 3
            # 或运算，找是否存在当前层的三个anchor是最大的iou的候选anchor
            # self.anch_mask 是当前层使用的anchor编号
            best_n_mask = ((best_n_all == self.anch_mask[0]) | (
                best_n_all == self.anch_mask[1]) | (best_n_all == self.anch_mask[2]))
            # best_n_mask.shape[n,1]
            # 把GT框的左上角坐标存入truth_box
            truth_box[:n, 0] = truth_x_all[b, :n]
            truth_box[:n, 1] = truth_y_all[b, :n]

            # pred.shape[3,fsize,fsize,4] 4: 左上角坐标，w, h
            pred_ious = bboxes_iou(
                pred[b].contiguous().view(-1, 4), truth_box, xyxy=False) # 计算所有预测框与all GT框的iou

            pred_best_iou, _ = pred_ious.max(dim=1) # 取每一行里最大的anchor，即即找到每一个预测框对应最大IOU的GT框
            # 过滤掉不符合条件的框
            pred_best_iou = (pred_best_iou > self.ignore_thre)
            # reshape to origin shape [3, fsize, fsize]
            pred_best_iou = pred_best_iou.view(pred[b].shape[:3])
            # set mask to zero (ignore) if pred matches truth
            # obj_mask[b] = 1 - pred_best_iou
            obj_mask[b] = ~pred_best_iou
            if sum(best_n_mask) == 0:
                continue
            # 这里范围是GT框个数 best_n.shape[n,1],n 指真实GT框的数量，都经过3取余
            # best_n_mask.shape[n,1]，找是否存在当前层的三个anchor是最大的iou的候选anchor
            for ti in range(best_n.shape[0]):
                if best_n_mask[ti] == 1:
                    # truth_i,truth_j真实框左上角坐标
                    i, j = truth_i[ti], truth_j[ti] # 取真实label的左上角坐标
                    a = best_n[ti] # 取匹配上的anchor的index
                    obj_mask[b, a, j, i] = 1 # TODO:?
                    tgt_mask[b, a, j, i, :] = 1 # TODO: ?
                    target[b, a, j, i, 0] = truth_x_all[b, ti] - \
                        truth_x_all[b, ti].to(torch.int16).to(torch.float)
                    target[b, a, j, i, 1] = truth_y_all[b, ti] - \
                        truth_y_all[b, ti].to(torch.int16).to(torch.float)
                    target[b, a, j, i, 2] = torch.log(
                        truth_w_all[b, ti] / torch.Tensor(self.masked_anchors)[best_n[ti], 0] + 1e-16)
                    target[b, a, j, i, 3] = torch.log(
                        truth_h_all[b, ti] / torch.Tensor(self.masked_anchors)[best_n[ti], 1] + 1e-16)
                    target[b, a, j, i, 4] = 1
                    target[b, a, j, i, 5 + labels[b, ti,
                                                  0].to(torch.int16).numpy()] = 1
                    tgt_scale[b, a, j, i, :] = torch.sqrt(
                        2 - truth_w_all[b, ti] * truth_h_all[b, ti] / fsize / fsize)

        # loss calculation

        output[..., 4] *= obj_mask
        output[..., np.r_[0:4, 5:n_ch]] *= tgt_mask
        output[..., 2:4] *= tgt_scale

        target[..., 4] *= obj_mask
        target[..., np.r_[0:4, 5:n_ch]] *= tgt_mask
        target[..., 2:4] *= tgt_scale

        bceloss = nn.BCELoss(weight=tgt_scale*tgt_scale,
                             size_average=False)  # weighted BCEloss
        loss_xy = bceloss(output[..., :2], target[..., :2])
        loss_wh = self.l2_loss(output[..., 2:4], target[..., 2:4]) / 2
        loss_obj = self.bce_loss(output[..., 4], target[..., 4])
        loss_cls = self.bce_loss(output[..., 5:], target[..., 5:])
        loss_l2 = self.l2_loss(output, target)

        loss = loss_xy + loss_wh + loss_obj + loss_cls

        return loss, loss_xy, loss_wh, loss_obj, loss_cls, loss_l2
