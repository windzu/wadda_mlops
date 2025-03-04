# Copyright (c) OpenMMLab. All rights reserved.
import gc
import io as sysio

import numba
import numpy as np


@numba.jit
def get_thresholds(scores: np.ndarray, num_gt, num_sample_pts=41):
    scores.sort()
    scores = scores[::-1]
    current_recall = 0
    thresholds = []
    for i, score in enumerate(scores):
        l_recall = (i + 1) / num_gt
        if i < (len(scores) - 1):
            r_recall = (i + 2) / num_gt
        else:
            r_recall = l_recall
        if ((r_recall - current_recall) < (current_recall - l_recall)) and (i < (len(scores) - 1)):
            continue
        # recall = l_recall
        thresholds.append(score)
        current_recall += 1 / (num_sample_pts - 1.0)
    return thresholds


@numba.jit(nopython=True)
def image_box_overlap(boxes, query_boxes, criterion=-1):
    N = boxes.shape[0]
    K = query_boxes.shape[0]
    overlaps = np.zeros((N, K), dtype=boxes.dtype)
    for k in range(K):
        qbox_area = (query_boxes[k, 2] - query_boxes[k, 0]) * (query_boxes[k, 3] - query_boxes[k, 1])
        for n in range(N):
            iw = min(boxes[n, 2], query_boxes[k, 2]) - max(boxes[n, 0], query_boxes[k, 0])
            if iw > 0:
                ih = min(boxes[n, 3], query_boxes[k, 3]) - max(boxes[n, 1], query_boxes[k, 1])
                if ih > 0:
                    if criterion == -1:
                        ua = (boxes[n, 2] - boxes[n, 0]) * (boxes[n, 3] - boxes[n, 1]) + qbox_area - iw * ih
                    elif criterion == 0:
                        ua = (boxes[n, 2] - boxes[n, 0]) * (boxes[n, 3] - boxes[n, 1])
                    elif criterion == 1:
                        ua = qbox_area
                    else:
                        ua = 1.0
                    overlaps[n, k] = iw * ih / ua
    return overlaps


def bev_box_overlap(boxes, qboxes, criterion=-1):
    from .rotate_iou import rotate_iou_gpu_eval

    riou = rotate_iou_gpu_eval(boxes, qboxes, criterion)
    return riou


@numba.jit(nopython=True, parallel=True)
def d3_box_overlap_kernel(boxes, qboxes, rinc, criterion=-1):
    # ONLY support overlap in CAMERA, not lidar.
    # TODO: change to use prange for parallel mode, should check the difference
    N, K = boxes.shape[0], qboxes.shape[0]
    for i in numba.prange(N):
        for j in numba.prange(K):
            if rinc[i, j] > 0:
                # iw = (min(boxes[i, 1] + boxes[i, 4], qboxes[j, 1] +
                #         qboxes[j, 4]) - max(boxes[i, 1], qboxes[j, 1]))
                iw = min(boxes[i, 1], qboxes[j, 1]) - max(boxes[i, 1] - boxes[i, 4], qboxes[j, 1] - qboxes[j, 4])

                if iw > 0:
                    area1 = boxes[i, 3] * boxes[i, 4] * boxes[i, 5]
                    area2 = qboxes[j, 3] * qboxes[j, 4] * qboxes[j, 5]
                    inc = iw * rinc[i, j]
                    if criterion == -1:
                        ua = area1 + area2 - inc
                    elif criterion == 0:
                        ua = area1
                    elif criterion == 1:
                        ua = area2
                    else:
                        ua = inc
                    rinc[i, j] = inc / ua
                else:
                    rinc[i, j] = 0.0


def d3_box_overlap(boxes, qboxes, criterion=-1):
    from .rotate_iou import rotate_iou_gpu_eval

    rinc = rotate_iou_gpu_eval(boxes[:, [0, 2, 3, 5, 6]], qboxes[:, [0, 2, 3, 5, 6]], 2)
    d3_box_overlap_kernel(boxes, qboxes, rinc, criterion)
    return rinc


@numba.jit(nopython=True)
def compute_statistics_jit(
    overlaps,
    gt_datas,
    dt_datas,
    ignored_gt,
    ignored_det,
    dc_bboxes,
    metric,
    min_overlap,
    thresh=0,
    compute_fp=False,
    compute_aos=False,
):

    det_size = dt_datas.shape[0]
    gt_size = gt_datas.shape[0]
    dt_scores = dt_datas[:, -1]
    dt_alphas = dt_datas[:, 4]
    gt_alphas = gt_datas[:, 4]
    dt_bboxes = dt_datas[:, :4]
    # gt_bboxes = gt_datas[:, :4]

    assigned_detection = [False] * det_size
    ignored_threshold = [False] * det_size
    if compute_fp:
        for i in range(det_size):
            if dt_scores[i] < thresh:
                ignored_threshold[i] = True
    NO_DETECTION = -10000000
    tp, fp, fn, similarity = 0, 0, 0, 0
    # thresholds = [0.0]
    # delta = [0.0]
    thresholds = np.zeros((gt_size,))
    thresh_idx = 0
    delta = np.zeros((gt_size,))
    delta_idx = 0
    for i in range(gt_size):
        if ignored_gt[i] == -1:
            continue
        det_idx = -1
        valid_detection = NO_DETECTION
        max_overlap = 0
        assigned_ignored_det = False

        for j in range(det_size):
            if ignored_det[j] == -1:
                continue
            if assigned_detection[j]:
                continue
            if ignored_threshold[j]:
                continue
            overlap = overlaps[j, i]
            dt_score = dt_scores[j]

            if not compute_fp and (overlap > min_overlap) and dt_score > valid_detection:
                det_idx = j
                valid_detection = dt_score
            elif (
                compute_fp
                and (overlap > min_overlap)
                and (overlap > max_overlap or assigned_ignored_det)
                and ignored_det[j] == 0
            ):
                max_overlap = overlap
                det_idx = j
                valid_detection = 1
                assigned_ignored_det = False
            elif compute_fp and (overlap > min_overlap) and (valid_detection == NO_DETECTION) and ignored_det[j] == 1:
                det_idx = j
                valid_detection = 1
                assigned_ignored_det = True

        if (valid_detection == NO_DETECTION) and ignored_gt[i] == 0:
            fn += 1
        elif (valid_detection != NO_DETECTION) and (ignored_gt[i] == 1 or ignored_det[det_idx] == 1):
            assigned_detection[det_idx] = True
        elif valid_detection != NO_DETECTION:
            tp += 1
            # thresholds.append(dt_scores[det_idx])
            thresholds[thresh_idx] = dt_scores[det_idx]
            thresh_idx += 1
            if compute_aos:
                # delta.append(gt_alphas[i] - dt_alphas[det_idx])
                delta[delta_idx] = gt_alphas[i] - dt_alphas[det_idx]
                delta_idx += 1

            assigned_detection[det_idx] = True
    if compute_fp:
        for i in range(det_size):
            if not (assigned_detection[i] or ignored_det[i] == -1 or ignored_det[i] == 1 or ignored_threshold[i]):
                fp += 1
        nstuff = 0
        if metric == 0:
            overlaps_dt_dc = image_box_overlap(dt_bboxes, dc_bboxes, 0)
            for i in range(dc_bboxes.shape[0]):
                for j in range(det_size):
                    if assigned_detection[j]:
                        continue
                    if ignored_det[j] == -1 or ignored_det[j] == 1:
                        continue
                    if ignored_threshold[j]:
                        continue
                    if overlaps_dt_dc[j, i] > min_overlap:
                        assigned_detection[j] = True
                        nstuff += 1
        fp -= nstuff
        if compute_aos:
            tmp = np.zeros((fp + delta_idx,))
            # tmp = [0] * fp
            for i in range(delta_idx):
                tmp[i + fp] = (1.0 + np.cos(delta[i])) / 2.0
                # tmp.append((1.0 + np.cos(delta[i])) / 2.0)
            # assert len(tmp) == fp + tp
            # assert len(delta) == tp
            if tp > 0 or fp > 0:
                similarity = np.sum(tmp)
            else:
                similarity = -1
    return tp, fp, fn, similarity, thresholds[:thresh_idx]


def get_split_parts(num, num_part):
    same_part = num // num_part
    remain_num = num % num_part
    if remain_num == 0:
        return [same_part] * num_part
    else:
        return [same_part] * num_part + [remain_num]


@numba.jit(nopython=True)
def fused_compute_statistics(
    overlaps,
    pr,
    gt_nums,
    dt_nums,
    dc_nums,
    gt_datas,
    dt_datas,
    dontcares,
    ignored_gts,
    ignored_dets,
    metric,
    min_overlap,
    thresholds,
    compute_aos=False,
):
    gt_num = 0
    dt_num = 0
    dc_num = 0
    for i in range(gt_nums.shape[0]):
        for t, thresh in enumerate(thresholds):
            overlap = overlaps[dt_num : dt_num + dt_nums[i], gt_num : gt_num + gt_nums[i]]

            gt_data = gt_datas[gt_num : gt_num + gt_nums[i]]
            dt_data = dt_datas[dt_num : dt_num + dt_nums[i]]
            ignored_gt = ignored_gts[gt_num : gt_num + gt_nums[i]]
            ignored_det = ignored_dets[dt_num : dt_num + dt_nums[i]]
            dontcare = dontcares[dc_num : dc_num + dc_nums[i]]
            tp, fp, fn, similarity, _ = compute_statistics_jit(
                overlap,
                gt_data,
                dt_data,
                ignored_gt,
                ignored_det,
                dontcare,
                metric,
                min_overlap=min_overlap,
                thresh=thresh,
                compute_fp=True,
                compute_aos=compute_aos,
            )
            pr[t, 0] += tp
            pr[t, 1] += fp
            pr[t, 2] += fn
            if similarity != -1:
                pr[t, 3] += similarity
        gt_num += gt_nums[i]
        dt_num += dt_nums[i]
        dc_num += dc_nums[i]


def calculate_iou_partly(gt_annos, dt_annos, metric, num_parts=50):
    """一个通过gpu并行计算从而快速批量计算的iou算法 , num_parts是并行的任务数量
    NOTE : 此算法在获取2dbbox 3dbbox或者2dbevbbox的时候,默认的顺序是相机坐标系
    TODO : 具体如何快速批量计算我还没仔细看,据说相交与cpu串行方式500x加速,参考自kitti中

    Args:
        gt_annos (dict): 格式遵循自定义lidar格式,包含location、dimensions、rotation_y 信息(camera坐标系下)
        dt_annos (dict): 格式遵循自定义lidar格式,包含location、dimensions、rotation_y 信息(camera坐标系下)
        metric (int): Eval type. 1: bev, 2: 3d.
        num_parts (int): A parameter for fast calculate algorithm.
    """
    assert len(gt_annos) == len(dt_annos)
    total_dt_num = np.stack([len(a["name"]) for a in dt_annos], 0)
    total_gt_num = np.stack([len(a["name"]) for a in gt_annos], 0)
    num_examples = len(gt_annos)
    split_parts = get_split_parts(num_examples, num_parts)
    parted_overlaps = []
    example_idx = 0

    for num_part in split_parts:
        gt_annos_part = gt_annos[example_idx : example_idx + num_part]
        dt_annos_part = dt_annos[example_idx : example_idx + num_part]
        if metric == 1:
            loc = np.concatenate([a["location"][:, [0, 2]] for a in gt_annos_part], 0)
            dims = np.concatenate([a["dimensions"][:, [0, 2]] for a in gt_annos_part], 0)
            rots = np.concatenate([a["rotation_y"] for a in gt_annos_part], 0)
            gt_boxes = np.concatenate([loc, dims, rots[..., np.newaxis]], axis=1)
            loc = np.concatenate([a["location"][:, [0, 2]] for a in dt_annos_part], 0)
            dims = np.concatenate([a["dimensions"][:, [0, 2]] for a in dt_annos_part], 0)
            rots = np.concatenate([a["rotation_y"] for a in dt_annos_part], 0)
            dt_boxes = np.concatenate([loc, dims, rots[..., np.newaxis]], axis=1)
            overlap_part = bev_box_overlap(gt_boxes, dt_boxes).astype(np.float64)
        elif metric == 2:
            loc = np.concatenate([a["location"] for a in gt_annos_part], 0)
            dims = np.concatenate([a["dimensions"] for a in gt_annos_part], 0)
            rots = np.concatenate([a["rotation_y"] for a in gt_annos_part], 0)
            gt_boxes = np.concatenate([loc, dims, rots[..., np.newaxis]], axis=1)
            loc = np.concatenate([a["location"] for a in dt_annos_part], 0)
            dims = np.concatenate([a["dimensions"] for a in dt_annos_part], 0)
            rots = np.concatenate([a["rotation_y"] for a in dt_annos_part], 0)
            dt_boxes = np.concatenate([loc, dims, rots[..., np.newaxis]], axis=1)
            overlap_part = d3_box_overlap(gt_boxes, dt_boxes).astype(np.float64)
        else:
            raise ValueError("unknown metric")
        parted_overlaps.append(overlap_part)
        example_idx += num_part
    overlaps = []
    example_idx = 0
    for j, num_part in enumerate(split_parts):
        gt_annos_part = gt_annos[example_idx : example_idx + num_part]
        dt_annos_part = dt_annos[example_idx : example_idx + num_part]
        gt_num_idx, dt_num_idx = 0, 0
        for i in range(num_part):
            gt_box_num = total_gt_num[example_idx + i]
            dt_box_num = total_dt_num[example_idx + i]
            overlaps.append(
                parted_overlaps[j][gt_num_idx : gt_num_idx + gt_box_num, dt_num_idx : dt_num_idx + dt_box_num]
            )
            gt_num_idx += gt_box_num
            dt_num_idx += dt_box_num
        example_idx += num_part

    return overlaps, parted_overlaps, total_gt_num, total_dt_num


def _prepare_data(gt_annos, dt_annos, current_class, difficulty):
    gt_datas_list = []
    dt_datas_list = []
    total_dc_num = []
    ignored_gts, ignored_dets, dontcares = [], [], []
    total_num_valid_gt = 0
    for i in range(len(gt_annos)):

        # 不需要clean data，伪造一个clean data
        # num_valid_gt, ignored_gt, ignored_det, dc_bboxes = rets
        num_valid_gt = len(gt_annos[i]["name"])  # num_valid_gt是每个example的gt的数量
        ignored_gt = [0] * len(gt_annos[i]["name"])  # 0表示没有被忽略
        ignored_det = [0] * len(dt_annos[i]["name"])  # 0表示没有被忽略
        dc_bboxes = []  # 空表示没有dont care的类别被忽略

        ignored_gts.append(np.array(ignored_gt, dtype=np.int64))
        ignored_dets.append(np.array(ignored_det, dtype=np.int64))
        if len(dc_bboxes) == 0:
            dc_bboxes = np.zeros((0, 4)).astype(np.float64)
        else:
            dc_bboxes = np.stack(dc_bboxes, 0).astype(np.float64)
        total_dc_num.append(dc_bboxes.shape[0])
        dontcares.append(dc_bboxes)
        total_num_valid_gt += num_valid_gt
        gt_datas = np.concatenate([gt_annos[i]["bbox"], gt_annos[i]["alpha"][..., np.newaxis]], 1)
        dt_datas = np.concatenate(
            [dt_annos[i]["bbox"], dt_annos[i]["alpha"][..., np.newaxis], dt_annos[i]["score"][..., np.newaxis]], 1
        )
        gt_datas_list.append(gt_datas)
        dt_datas_list.append(dt_datas)
    total_dc_num = np.stack(total_dc_num, axis=0)
    return (gt_datas_list, dt_datas_list, ignored_gts, ignored_dets, dontcares, total_dc_num, total_num_valid_gt)


def eval_class(
    gt_annos,
    dt_annos,
    current_classes,
    difficultys,
    metric,
    min_overlaps,
    compute_aos=False,
    num_parts=200,
):
    """自定义的lidar eval函数,参考自kitti,目前仅支持bev 3d的eval

    Args:
        gt_annos (dict): Must from get_label_annos() in kitti_common.py.
        dt_annos (dict): Must from get_label_annos() in kitti_common.py.
        current_classes (list[int]): 参与评估的类别 class_id list
        difficultys (list[int]): Eval difficulty, 0: easy, 1: normal, 2: hard
        metric (int): Eval type. 1: bev, 2: 3d
        min_overlaps (float): Min overlap. format: [num_overlap, metric, class].
        num_parts (int): A parameter for fast calculate algorithm

    Returns:
        dict[str, np.ndarray]: recall, precision and aos
    """
    assert len(gt_annos) == len(dt_annos)
    num_examples = len(gt_annos)
    if num_examples < num_parts:
        num_parts = num_examples
    split_parts = get_split_parts(num_examples, num_parts)  # 返回一个list[int] 不清楚是什么意思

    rets = calculate_iou_partly(dt_annos, gt_annos, metric, num_parts)
    overlaps, parted_overlaps, total_dt_num, total_gt_num = rets
    N_SAMPLE_PTS = 41
    num_minoverlap = len(min_overlaps)
    num_class = len(current_classes)
    num_difficulty = len(difficultys)
    precision = np.zeros([num_class, num_difficulty, num_minoverlap, N_SAMPLE_PTS])
    recall = np.zeros([num_class, num_difficulty, num_minoverlap, N_SAMPLE_PTS])
    aos = np.zeros([num_class, num_difficulty, num_minoverlap, N_SAMPLE_PTS])
    for m, current_class in enumerate(current_classes):
        for idx_l, difficulty in enumerate(difficultys):
            rets = _prepare_data(gt_annos, dt_annos, current_class, difficulty)
            (
                gt_datas_list,
                dt_datas_list,
                ignored_gts,
                ignored_dets,
                dontcares,
                total_dc_num,
                total_num_valid_gt,
            ) = rets
            for k, min_overlap in enumerate(min_overlaps[:, metric, m]):

                thresholdss = []
                for i in range(len(gt_annos)):

                    rets = compute_statistics_jit(
                        overlaps[i],
                        gt_datas_list[i],
                        dt_datas_list[i],
                        ignored_gts[i],
                        ignored_dets[i],
                        dontcares[i],
                        metric,
                        min_overlap=min_overlap,
                        thresh=0.0,
                        compute_fp=False,
                    )
                    tp, fp, fn, similarity, thresholds = rets
                    thresholdss += thresholds.tolist()
                thresholdss = np.array(thresholdss)
                thresholds = get_thresholds(thresholdss, total_num_valid_gt)
                thresholds = np.array(thresholds)
                pr = np.zeros([len(thresholds), 4])
                idx = 0
                for j, num_part in enumerate(split_parts):
                    gt_datas_part = np.concatenate(gt_datas_list[idx : idx + num_part], 0)
                    dt_datas_part = np.concatenate(dt_datas_list[idx : idx + num_part], 0)
                    dc_datas_part = np.concatenate(dontcares[idx : idx + num_part], 0)
                    ignored_dets_part = np.concatenate(ignored_dets[idx : idx + num_part], 0)
                    ignored_gts_part = np.concatenate(ignored_gts[idx : idx + num_part], 0)
                    fused_compute_statistics(
                        parted_overlaps[j],
                        pr,
                        total_gt_num[idx : idx + num_part],
                        total_dt_num[idx : idx + num_part],
                        total_dc_num[idx : idx + num_part],
                        gt_datas_part,
                        dt_datas_part,
                        dc_datas_part,
                        ignored_gts_part,
                        ignored_dets_part,
                        metric,
                        min_overlap=min_overlap,
                        thresholds=thresholds,
                        compute_aos=compute_aos,
                    )
                    idx += num_part
                for i in range(len(thresholds)):
                    recall[m, idx_l, k, i] = pr[i, 0] / (pr[i, 0] + pr[i, 2])
                    precision[m, idx_l, k, i] = pr[i, 0] / (pr[i, 0] + pr[i, 1])
                    if compute_aos:
                        aos[m, idx_l, k, i] = pr[i, 3] / (pr[i, 0] + pr[i, 1])
                for i in range(len(thresholds)):
                    precision[m, idx_l, k, i] = np.max(precision[m, idx_l, k, i:], axis=-1)
                    recall[m, idx_l, k, i] = np.max(recall[m, idx_l, k, i:], axis=-1)
                    if compute_aos:
                        aos[m, idx_l, k, i] = np.max(aos[m, idx_l, k, i:], axis=-1)
    ret_dict = {
        "recall": recall,
        "precision": precision,
        "orientation": aos,
    }

    # clean temp variables
    del overlaps
    del parted_overlaps

    gc.collect()
    return ret_dict


def get_mAP11(prec):
    sums = 0
    for i in range(0, prec.shape[-1], 4):
        sums = sums + prec[..., i]
    return sums / 11 * 100


def get_mAP40(prec):
    sums = 0
    for i in range(1, prec.shape[-1]):
        sums = sums + prec[..., i]
    return sums / 40 * 100


def print_str(value, *arg, sstream=None):
    if sstream is None:
        sstream = sysio.StringIO()
    sstream.truncate(0)
    sstream.seek(0)
    print(value, *arg, file=sstream)
    return sstream.getvalue()


def do_eval(gt_annos, dt_annos, current_classes, min_overlaps, eval_types=["bev", "3d"]):
    # min_overlaps: [num_minoverlap, metric, num_class]
    difficultys = [0, 1, 2]

    mAP11_bev = None
    mAP40_bev = None
    if "bev" in eval_types:
        ret = eval_class(gt_annos, dt_annos, current_classes, difficultys, 1, min_overlaps)
        mAP11_bev = get_mAP11(ret["precision"])
        mAP40_bev = get_mAP40(ret["precision"])

    mAP11_3d = None
    mAP40_3d = None
    if "3d" in eval_types:
        ret = eval_class(gt_annos, dt_annos, current_classes, difficultys, 2, min_overlaps)
        mAP11_3d = get_mAP11(ret["precision"])
        mAP40_3d = get_mAP40(ret["precision"])
    return (mAP11_bev, mAP11_3d, mAP40_bev, mAP40_3d)


def usd_eval(gt_annos, dt_annos, current_classes, eval_types=["bev", "3d"]):
    """usd数据集的eval方法
    NOTE : 修改自kitti的 evaluation. kitti中支持2dbbox 3dbbox_bev 3d 三种评估方式,本方法仅支持 bev 和 3d
    NOTE : 因为评估方式不同写起来很费事,所以首先将usd格式的标注数据转换为kitti格式的标注数据,
    然后就直接使用原始的kitti的评估方式,但是因为kitti还提供了bbox的信息,而我们没有此部分的真实数据
    所以要将与bbox相关的部分删除

    TODO : 当前的eval仅支持对3d检测的eval

    Args:
        gt_annos (list[dict]): Contain gt information of each sample.
        dt_annos (list[dict]): Contain detected information of each sample.
        current_classes (list[str]): 用于eval的class_name list.
        eval_types (list[str], optional): Types to eval. Defaults to ['bev', '3d'].

    Returns:
        tuple: String and dict of evaluation results.
    """

    def convert_lidar_annos_to_kitti_annos(lidar_annos):
        """将usd格式的标注数据转换为kitti格式的标注数据
        两者的区别主要在于:
            1. usd格式的标注数据中location dimensions rotation_y都是在lidar坐标系下的,
        而kitti格式的标注数据中location dimensions rotation_y都是在camera坐标系下的
            2. kitti格式的标注中有 truncated occluded alpha bbox的信息,而lidar格式的标注中没有
        """
        for anno in lidar_annos:
            object_nums = len(anno["name"])

            # 将lidar坐标系下的3d bbox标注转换为camera坐标系下的3d bbox标注
            # camera x y z equal lidar -y -z x
            anno["location"] = anno["location"][:, [1, 2, 0]] * [-1, -1, 1]
            anno["dimensions"] = anno["dimensions"][:, [1, 2, 0]]
            anno["rotation_y"] = anno["rotation_y"] + np.pi / 2

            # 下面的字段缺少真实的信息,所以用0填充
            anno["truncated"] = np.zeros((object_nums,))
            anno["occluded"] = np.zeros((object_nums,))
            anno["alpha"] = np.zeros((object_nums,))
            anno["bbox"] = np.zeros((object_nums, 4))

    convert_lidar_annos_to_kitti_annos(gt_annos)
    convert_lidar_annos_to_kitti_annos(dt_annos)

    assert len(eval_types) > 0, "must contain at least one evaluation type"

    # 定义eval时各个类别的iou阈值、评估难度等
    # TODO ： 具体的分析每一个参数的含义
    # NOTE ：由于不同类别的size不一样，如果采用同一个iou阈值会导致某些类别的某些框被评估为0,
    # 例如: 在 overlap 为0.7时候 car的 overlap可以设置为0.7
    # 但是person的 overlap可以设置为0.5(不然person类别的框会很容易被评估为0)
    nus_overlap_0_7 = np.array(
        [
            [0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7],  # bbox metric
            [0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7],  # bev
            [0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7],  # 3d
        ]
    )
    nus_overlap_0_5 = np.array(
        [
            [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],  # bbox metric
            [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],  # bev
            [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],  # 3d
        ]
    )
    min_overlaps = np.stack([nus_overlap_0_7, nus_overlap_0_5], axis=0)  # [2, 3, 10]

    # init class_to_name and name_to_class
    class_to_name = {
        0: "car",
        1: "truck",
        2: "trailer",
        3: "bus",
        4: "construction_vehicle",
        5: "bicycle",
        6: "motorcycle",
        7: "pedestrian",
        8: "traffic_cone",
        9: "barrier",
    }
    name_to_class = {v: n for n, v in class_to_name.items()}

    if not isinstance(current_classes, (list, tuple)):
        current_classes = [current_classes]
    current_classes_int = []
    for curcls in current_classes:
        if isinstance(curcls, str):
            current_classes_int.append(name_to_class[curcls])
        else:
            current_classes_int.append(curcls)
    current_classes = current_classes_int

    # 根据参与eval的所有类的label_id,获取其对应的预设的iou阈值
    min_overlaps = min_overlaps[:, :, current_classes]

    result = ""

    mAP11_bev, mAP11_3d, mAP40_bev, mAP40_3d = do_eval(gt_annos, dt_annos, current_classes, min_overlaps, eval_types)

    ret_dict = {}
    difficulty = ["easy", "moderate", "hard"]

    # calculate AP11
    result += "\n----------- AP11 Results ------------\n\n"
    for j, curcls in enumerate(current_classes):
        # mAP threshold array: [num_minoverlap, metric, class]
        # mAP result: [num_class, num_diff, num_minoverlap]
        curcls_name = class_to_name[curcls]
        for i in range(min_overlaps.shape[0]):
            # prepare results for print
            result += "{} AP11@{:.2f}, {:.2f}, {:.2f}:\n".format(curcls_name, *min_overlaps[i, :, j])
            if mAP11_bev is not None:
                result += "bev  AP11:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP11_bev[j, :, i])
            if mAP11_3d is not None:
                result += "3d   AP11:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP11_3d[j, :, i])

            # prepare results for logger
            for idx in range(3):
                if i == 0:
                    postfix = f"{difficulty[idx]}_strict"
                else:
                    postfix = f"{difficulty[idx]}_loose"
                prefix = f"lidar/{curcls_name}"
                if mAP11_3d is not None:
                    ret_dict[f"{prefix}_3D_AP11_{postfix}"] = mAP11_3d[j, idx, i]
                if mAP11_bev is not None:
                    ret_dict[f"{prefix}_BEV_AP11_{postfix}"] = mAP11_bev[j, idx, i]

    # calculate mAP11 over all classes if there are multiple classes
    if len(current_classes) > 1:
        # prepare results for print
        result += "\nOverall AP11@{}, {}, {}:\n".format(*difficulty)
        if mAP11_bev is not None:
            mAP11_bev = mAP11_bev.mean(axis=0)
            result += "bev  AP11:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP11_bev[:, 0])
        if mAP11_3d is not None:
            mAP11_3d = mAP11_3d.mean(axis=0)
            result += "3d   AP11:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP11_3d[:, 0])

        # prepare results for logger
        for idx in range(3):
            postfix = f"{difficulty[idx]}"
            if mAP11_3d is not None:
                ret_dict[f"KITTI/Overall_3D_AP11_{postfix}"] = mAP11_3d[idx, 0]
            if mAP11_bev is not None:
                ret_dict[f"KITTI/Overall_BEV_AP11_{postfix}"] = mAP11_bev[idx, 0]

    # Calculate AP40
    result += "\n----------- AP40 Results ------------\n\n"
    for j, curcls in enumerate(current_classes):
        # mAP threshold array: [num_minoverlap, metric, class]
        # mAP result: [num_class, num_diff, num_minoverlap]
        curcls_name = class_to_name[curcls]
        for i in range(min_overlaps.shape[0]):
            # prepare results for print
            result += "{} AP40@{:.2f}, {:.2f}, {:.2f}:\n".format(curcls_name, *min_overlaps[i, :, j])
            if mAP40_bev is not None:
                result += "bev  AP40:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP40_bev[j, :, i])
            if mAP40_3d is not None:
                result += "3d   AP40:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP40_3d[j, :, i])

            # prepare results for logger
            for idx in range(3):
                if i == 0:
                    postfix = f"{difficulty[idx]}_strict"
                else:
                    postfix = f"{difficulty[idx]}_loose"
                prefix = f"LIDAR/{curcls_name}"
                if mAP40_3d is not None:
                    ret_dict[f"{prefix}_3D_AP40_{postfix}"] = mAP40_3d[j, idx, i]
                if mAP40_bev is not None:
                    ret_dict[f"{prefix}_BEV_AP40_{postfix}"] = mAP40_bev[j, idx, i]

    # calculate mAP40 over all classes if there are multiple classes
    if len(current_classes) > 1:
        # prepare results for print
        result += "\nOverall AP40@{}, {}, {}:\n".format(*difficulty)
        if mAP40_bev is not None:
            mAP40_bev = mAP40_bev.mean(axis=0)
            result += "bev  AP40:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP40_bev[:, 0])
        if mAP40_3d is not None:
            mAP40_3d = mAP40_3d.mean(axis=0)
            result += "3d   AP40:{:.4f}, {:.4f}, {:.4f}\n".format(*mAP40_3d[:, 0])

        # prepare results for logger
        for idx in range(3):
            postfix = f"{difficulty[idx]}"
            if mAP40_3d is not None:
                ret_dict[f"LIDAR/Overall_3D_AP40_{postfix}"] = mAP40_3d[idx, 0]
            if mAP40_bev is not None:
                ret_dict[f"LIDAR/Overall_BEV_AP40_{postfix}"] = mAP40_bev[idx, 0]

    return result, ret_dict
