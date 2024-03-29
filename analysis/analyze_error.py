#! /usr/bin/env python
# coding=utf-8
#===============================================================
# Copyright (C) 2019 * Ltd. All rights reserved.
#
#   Editor      : VIM
#   File name   : evaluate.py
#   Author      : YunYang1994
#   Created date: 2019-02-21 15:30:26
#   Description :
#
#================================================================

import cv2
import os
import shutil
import numpy as np
import tensorflow as tf
import core.utils as utils
from core.config import cfg
from core.yolov3 import YOLOV3
from core.dataset import Dataset

class YoloTest(object):
    def __init__(self):
        self.input_size       = cfg.TEST.INPUT_SIZE
        self.anchor_per_scale = cfg.YOLO.ANCHOR_PER_SCALE
        self.classes          = utils.read_class_names(cfg.YOLO.CLASSES)
        self.num_classes      = len(self.classes)
        self.anchors          = np.array(utils.get_anchors(cfg.YOLO.ANCHORS))
        self.score_threshold  = cfg.TEST.SCORE_THRESHOLD
        self.iou_threshold    = cfg.TEST.IOU_THRESHOLD
        self.moving_ave_decay = cfg.YOLO.MOVING_AVE_DECAY
        self.annotation_path  = cfg.TEST.ANNOT_PATH
        self.weight_file      = cfg.TEST.WEIGHT_FILE
        self.write_image      = cfg.TEST.WRITE_IMAGE
        self.write_image_path = cfg.TEST.WRITE_IMAGE_PATH
        self.show_label       = cfg.TEST.SHOW_LABEL

        with tf.name_scope('input'):
            self.input_data   = tf.placeholder(dtype=tf.float32, name='input_data')
            self.label_sbbox  = tf.placeholder(dtype=tf.float32, name='label_sbbox')
            self.label_mbbox  = tf.placeholder(dtype=tf.float32, name='label_mbbox')
            self.label_lbbox  = tf.placeholder(dtype=tf.float32, name='label_lbbox')
            self.true_sbboxes = tf.placeholder(dtype=tf.float32, name='sbboxes')
            self.true_mbboxes = tf.placeholder(dtype=tf.float32, name='mbboxes')
            self.true_lbboxes = tf.placeholder(dtype=tf.float32, name='lbboxes')
            self.trainable     = tf.placeholder(dtype=tf.bool, name='training')

        model = YOLOV3(self.input_data, self.trainable)
        self.pred_sbbox, self.pred_mbbox, self.pred_lbbox = model.pred_sbbox, model.pred_mbbox, model.pred_lbbox

        self.giou_loss, self.conf_loss, self.prob_loss = model.compute_loss(
                self.label_sbbox,  self.label_mbbox,  self.label_lbbox,
                self.true_sbboxes, self.true_mbboxes, self.true_lbboxes)
        self.loss = self.giou_loss + self.conf_loss + self.prob_loss

        with tf.name_scope('ema'):
            ema_obj = tf.train.ExponentialMovingAverage(self.moving_ave_decay)

        self.sess  = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))
        self.saver = tf.train.Saver(ema_obj.variables_to_restore())
        self.saver.restore(self.sess, self.weight_file)

    def predict(self, image, annotation):
        dataset_obj = Dataset('test')
        dataset_obj.train_input_size = cfg.TEST.INPUT_SIZE
        dataset_obj.strides = np.array(cfg.YOLO.STRIDES)
        dataset_obj.train_output_sizes = dataset_obj.train_input_size // dataset_obj.strides

        org_image = np.copy(image)
        org_h, org_w, _ = org_image.shape

        image_data = utils.image_preporcess(image, [self.input_size, self.input_size])
        image_data = image_data[np.newaxis, ...]

        _, gt_bboxes = dataset_obj.parse_annotation(annotation)
        label_sbbox, label_mbbox, label_lbbox, sbboxes, mbboxes, lbboxes = dataset_obj.preprocess_true_boxes(gt_bboxes)
        label_sbbox = label_sbbox[np.newaxis, ...]
        label_mbbox = label_mbbox[np.newaxis, ...]
        label_lbbox = label_lbbox[np.newaxis, ...]
        sbboxes = sbboxes[np.newaxis, ...]
        mbboxes = mbboxes[np.newaxis, ...]
        lbboxes = lbboxes[np.newaxis, ...]

        pred_sbbox, pred_mbbox, pred_lbbox, loss = self.sess.run(
            [self.pred_sbbox, self.pred_mbbox, self.pred_lbbox, self.loss],
            feed_dict={
                self.input_data: image_data,
                self.label_sbbox: label_sbbox,
                self.label_mbbox: label_mbbox,
                self.label_lbbox: label_lbbox,
                self.true_sbboxes: sbboxes,
                self.true_mbboxes: mbboxes,
                self.true_lbboxes: lbboxes,
                self.trainable: False
            }
        )

        pred_bbox = np.concatenate([np.reshape(pred_sbbox, (-1, 5 + self.num_classes)),
                                    np.reshape(pred_mbbox, (-1, 5 + self.num_classes)),
                                    np.reshape(pred_lbbox, (-1, 5 + self.num_classes))], axis=0)
        bboxes = utils.postprocess_boxes(pred_bbox, (org_h, org_w), self.input_size, self.score_threshold)
        bboxes = utils.nms(bboxes, self.iou_threshold)

        return bboxes, loss

    def evaluate(self):
        predicted_dir_path = './mAP/predicted'
        ground_truth_dir_path = './mAP/ground-truth'
        loss_dir_path = './mAP/loss'
        if os.path.exists(predicted_dir_path): shutil.rmtree(predicted_dir_path)
        if os.path.exists(ground_truth_dir_path): shutil.rmtree(ground_truth_dir_path)
        if os.path.exists(loss_dir_path): shutil.rmtree(loss_dir_path)
        if os.path.exists(self.write_image_path): shutil.rmtree(self.write_image_path)
        os.mkdir(predicted_dir_path)
        os.mkdir(ground_truth_dir_path)
        os.mkdir(loss_dir_path)
        os.mkdir(self.write_image_path)

        loss_summary_path = os.path.join(loss_dir_path, 'summary.txt')

        with open(self.annotation_path, 'r') as annotation_file:
            for num, line in enumerate(annotation_file):
                annotation = line.strip().split()
                image_path = annotation[0]
                image_name = image_path.split('/')[-1]
                image = cv2.imread(image_path)
                bbox_data_gt = np.array([list(map(int, box.split(','))) for box in annotation[1:]])

                if len(bbox_data_gt) == 0:
                    bboxes_gt=[]
                    classes_gt=[]
                else:
                    bboxes_gt, classes_gt = bbox_data_gt[:, :4], bbox_data_gt[:, 4]
                ground_truth_path = os.path.join(ground_truth_dir_path, str(num) + '.txt')

                print('=> ground truth of %s:' % image_name)
                num_bbox_gt = len(bboxes_gt)
                with open(ground_truth_path, 'w') as f:
                    for i in range(num_bbox_gt):
                        class_name = self.classes[classes_gt[i]]
                        xmin, ymin, xmax, ymax = list(map(str, bboxes_gt[i]))
                        bbox_mess = ' '.join([class_name, xmin, ymin, xmax, ymax]) + '\n'
                        f.write(bbox_mess)
                        print('\t' + str(bbox_mess).strip())
                print('=> predict result of %s:' % image_name)
                predict_result_path = os.path.join(predicted_dir_path, str(num) + '.txt')
                bboxes_pr, loss = self.predict(image, line)

                if self.write_image:
                    image = utils.draw_bbox(image, bboxes_pr, show_label=self.show_label)
                    cv2.imwrite(self.write_image_path+image_name, image)

                with open(predict_result_path, 'w') as f:
                    for bbox in bboxes_pr:
                        coor = np.array(bbox[:4], dtype=np.int32)
                        score = bbox[4]
                        class_ind = int(bbox[5])
                        class_name = self.classes[class_ind]
                        score = '%.4f' % score
                        xmin, ymin, xmax, ymax = list(map(str, coor))
                        bbox_mess = ' '.join([class_name, score, xmin, ymin, xmax, ymax]) + '\n'
                        f.write(bbox_mess)
                        print('\t' + str(bbox_mess).strip())

                loss_path = os.path.join(loss_dir_path, str(num) + '.txt')
                with open(loss_path, 'w') as f:
                    f.write("{:.6f}".format(loss))
                    print("loss: {:.6f}".format(loss))

                with open(loss_summary_path, 'a+') as f:
                    f.write("{} {} : {:.6f}\n".format(num, image_name, loss))


if __name__ == '__main__': YoloTest().evaluate()
