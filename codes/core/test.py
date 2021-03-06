# !/usr/bin/env python3
import os.path as osp
import pickle
import shutil
import tempfile
# import mmcv
# import torch
# import torch.distributed as dist
# from mmcv.runner import get_dist_info

import paddle


def single_gpu_test(model, data_loader, save_vididx=False):
    """Test model with a single gpu.
    This method tests model with a single gpu and displays test progress bar.
    Args:
        model (nn.Module): Model to be tested.
        data_loader (nn.Dataloader): Pytorch data loader.
    Returns:
        list: The prediction results.
    """
    model.eval()
    results = []
    vids = []
    dataset = data_loader.dataset
    prog_bar = mmcv.ProgressBar(len(dataset))
    for i, data in enumerate(data_loader):
        with paddle.no_grad():
            result = model(return_loss=False, return_numpy=False, **data)
        results.append(result)
        if save_vididx:
            vids.append(data['vid_idx'])
        # use the first key as main key to calculate the batch size
        batch_size = len(next(iter(data.values())))
        for _ in range(batch_size):
            prog_bar.update()
    if save_vididx:
        return results, vids
    else:
        return results


def multi_gpu_test(model, data_loader, tmpdir=None, gpu_collect=True, save_vididx=False):
    """Test model with multiple gpus.
    This method tests model with multiple gpus and collects the results
    under two different modes: gpu and cpu modes. By setting 'gpu_collect=True'
    it encodes results to gpu tensors and use gpu communication for results
    collection. On cpu mode it saves the results on different gpus to 'tmpdir'
    and collects them by the rank 0 worker.
    Args:
        model (nn.Module): Model to be tested.
        data_loader (nn.Dataloader): Pytorch data loader.
        tmpdir (str): Path of directory to save the temporary results from
            different gpus under cpu mode. Default: None
        gpu_collect (bool): Option to use either gpu or cpu to collect results.
            Default: True
    Returns:
        list: The prediction results.
    """
    model.eval()
    results = []
    vids = []
    dataset = data_loader.dataset
    rank, world_size = get_dist_info()
    if rank == 0:
        prog_bar = mmcv.ProgressBar(len(dataset))

    for data in data_loader:
        with paddle.no_grad():
            result = model(return_loss=False, **data)
        if save_vididx:
            vids.append(data['vid_idx'])
        results.append(result)
        if rank == 0:
            # use the first key as main key to calculate the batch size
            batch_size = len(next(iter(data.values())))
            for _ in range(batch_size * world_size):
                prog_bar.update()
    # collect results from all ranks
    if rank == 0:
        print('\nStarting to collect results!')
    if gpu_collect:
        results = collect_results_gpu(results, len(dataset))
    else:
        results = collect_results_cpu(results, len(dataset), tmpdir)
    vids = collect_results_gpu(vids, len(dataset))
    if save_vididx:
        return results, vids
    else:
        return results


def collect_results_cpu(result_part, size, tmpdir=None):
    """Collect results in cpu mode.
    It saves the results on different gpus to 'tmpdir' and collects
    them by the rank 0 worker.
    Args:
        result_part (list): Results to be collected
        size (int): Result size.
        tmpdir (str): Path of directory to save the temporary results from
            different gpus under cpu mode. Default: None
    Returns:
        list: Ordered results.
    """
    rank, world_size = get_dist_info()
    # create a tmp dir if it is not specified
    if tmpdir is None:
        MAX_LEN = 512
        # 32 is whitespace
        dir_tensor = paddle.full((MAX_LEN, ),
                                32,
                                dtype=paddle.uint8,
                                )
        if rank == 0:
            tmpdir = tempfile.mkdtemp()
            tmpdir = paddle.to_tensor(
                bytearray(tmpdir.encode()), dtype=paddle.uint8)
            dir_tensor[:len(tmpdir)] = tmpdir
        paddle.distributed.broadcast(dir_tensor, 0)
        tmpdir = dir_tensor.cpu().numpy().tobytes().decode().rstrip()
    else:
        mmcv.mkdir_or_exist(tmpdir)
    # dump the part result to the dir
    print('rank {} begin dump'.format(rank), flush=True)
    mmcv.dump(result_part, osp.join(tmpdir, 'part_{}.pkl'.format(rank)))
    print('rank {} finished dump'.format(rank), flush=True)
    paddle.distributed.barrier()
    # collect all parts
    if rank != 0:
        return None
    else:
        # load results of all parts from tmp dir
        part_list = []
        for i in range(world_size):
            part_file = osp.join(tmpdir, 'part_{}.pkl'.format(i))
            part_list.append(mmcv.load(part_file))
        # sort the results
        ordered_results = []
        for res in zip(*part_list):
            ordered_results.extend(list(res))
        # the dataloader may pad some samples
        ordered_results = ordered_results[:size]
        # remove tmp dir
        shutil.rmtree(tmpdir)
        return ordered_results


def collect_results_gpu(result_part, size):
    """Collect results in gpu mode.
    It encodes results to gpu tensors and use gpu communication for results
    collection.
    Args:
        result_part (list): Results to be collected
        size (int): Result size.
    Returns:
        list: Ordered results.
    """
    rank, world_size = get_dist_info()
    # dump result part to tensor with pickle
    part_tensor = paddle.to_tensor(
        bytearray(pickle.dumps(result_part)), dtype=paddle.uint8)
    # gather all result part tensor shape
    shape_tensor = paddle.to_tensor(part_tensor.shape)
    shape_list = [shape_tensor.clone() for _ in range(world_size)]
    paddle.distributed.all_gather(shape_list, shape_tensor)
    # padding result part tensor to max length
    shape_max = paddle.to_tensor(shape_list).max()
    part_send = paddle.zeros(shape_max, dtype=paddle.uint8)
    part_send[:shape_tensor[0]] = part_tensor
    part_recv_list = [
        part_tensor.new_zeros(shape_max) for _ in range(world_size)
    ]
    # gather all result part
    paddle.distributed.all_gather(part_recv_list, part_send)
    if rank == 0:
        part_list = []
        for recv, shape in zip(part_recv_list, shape_list):
            part_list.append(
                pickle.loads(recv[:shape[0]].cpu().numpy().tobytes()))
        # sort the results
        ordered_results = []
        for res in zip(*part_list):
            ordered_results.extend(list(res))
        # the dataloader may pad some samples
        ordered_results = ordered_results[:size]
        return ordered_results
