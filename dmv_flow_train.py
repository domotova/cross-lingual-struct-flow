from __future__ import print_function

import os
import argparse
import time
import sys
import pickle
import importlib

import torch
import numpy as np

from modules import ConlluData
import modules.dmv_flow_model as dmv
from modules import data_iter, \
                    read_conll, \
                    sents_to_vec, \
                    sents_to_tagid, \
                    to_input_tensor, \
                    generate_seed

from multilingual_trans.fasttext import FastVector

lr_decay = 0.5

def init_config():

    parser = argparse.ArgumentParser(description='dependency parsing')

    # train and test data
    parser.add_argument('--lang', type=str, help='language')

    # model config
    parser.add_argument('--model', choices=["gaussian", "nice", "lstmnice"], default='gaussian')
    parser.add_argument('--mode',
                         choices=['supervised_wpos', 'supervised_wopos', 'unsupervised', 'both', 'eval'],
                         default='supervised')

    # optimization params
    parser.add_argument('--opt', choices=['adam', 'sgd'], default='adam')
    parser.add_argument('--prior_lr', type=float, default=0.001)
    parser.add_argument('--proj_lr', type=float, default=0.001)

    # pretrained model options
    parser.add_argument('--load_nice', default='', type=str,
        help='load pretrained projection model, ignored by default')
    parser.add_argument('--load_gaussian', default='', type=str,
        help='load pretrained Gaussian model, ignored by default')
    parser.add_argument('--seed', default=783435, type=int, help='random seed')
    parser.add_argument('--set_seed', action='store_true', default=False, help='if set seed')

    # these are for slurm purpose to save model
    # they can also be used to run multiple random restarts with various settings,
    # to save models that can be identified with ids
    parser.add_argument('--jobid', type=int, default=0, help='slurm job id')
    parser.add_argument('--taskid', type=int, default=0, help='slurm task id')

    args = parser.parse_args()
    args.cuda = torch.cuda.is_available()

    save_dir = "dump_models/dmv"

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    id_ = "{}_{}_{}_{}_{}".format(args.lang, args.mode, args.model, args.jobid, args.taskid)
    save_path = os.path.join(save_dir, id_ + '.pt')
    args.save_path = save_path

    print("model save path: ", save_path)

    # load config file into args
    config_file = "config.config_{}".format(args.lang)
    params = importlib.import_module(config_file).params_dmv
    args = argparse.Namespace(**vars(args), **params)

    if args.set_seed:
        torch.manual_seed(args.seed)
        if args.cuda:
            torch.cuda.manual_seed(args.seed)
        np.random.seed(args.seed)

    print(args)

    return args


def main(args):

    word_vec_dict = FastVector(vector_file=args.vec_file)
    word_vec_dict.apply_transform(args.align_file)
    print('complete loading word vectors')

    device = torch.device("cuda" if args.cuda else "cpu")
    args.device = device

    if args.mode == "unsupervised":
        train_max_len = 1e3
    else:
        train_max_len = 1e3

    train_data = ConlluData(args.train_file, word_vec_dict,
            max_len=train_max_len, device=device,
            read_tree=(args.mode == "supervised_wopos"))
    pos_to_id = train_data.pos_to_id

    val_data = ConlluData(args.val_file, word_vec_dict,
            max_len=1e3, device=device, pos_to_id_dict=pos_to_id)
    test_data = ConlluData(args.test_file, word_vec_dict,
            max_len=1e3, device=device, pos_to_id_dict=pos_to_id)

    num_dims = len(train_data.embed[0][0])
    print('complete reading data')

    print("embedding dims {}".format(num_dims))
    print("{} pos tags".format(len(pos_to_id)))
    print("#train sentences: {}".format(train_data.length))
    print("#dev sentences: {}".format(val_data.length))
    print("#test sentences: {}".format(test_data.length))

    exclude_pos = [pos_to_id["PUNCT"], pos_to_id["SYM"]]
    model = dmv.DMVFlow(args, len(pos_to_id),
        num_dims, exclude_pos, word_vec_dict).to(device)

    init_seed = next(train_data.data_iter(args.batch_size))

    with torch.no_grad():
        model.init_params(init_seed, train_data)
    print('complete init')

    opt_dict = {"not_improved": 0, "lr": 0., "best_score": 0}

    if args.opt == "adam":
        prior_optimizer = torch.optim.Adam(model.prior_group, lr=args.prior_lr)
        proj_optimizer = torch.optim.Adam(model.proj_group, lr=args.proj_lr)
        opt_dict["lr"] = 1.
    elif args.opt == "sgd":
        prior_optimizer = torch.optim.SGD(model.prior_group, lr=1.)
        proj_optimizer = torch.optim.SGD(model. proj_group, lr=1.)
        opt_dict["lr"] = 1.
    else:
        raise ValueError("{} is not supported".format(args.opt))

    log_niter = (train_data.length//args.batch_size)//5
    # log_niter = 20
    report_ll = report_num_words = report_num_sents = epoch = train_iter = 0
    stop_avg_ll = stop_num_words = 0
    stop_avg_ll_last = 1
    dir_last = 0
    begin_time = time.time()
    # with torch.no_grad():
    #     directed = model.test(test_data)
    # print("TEST accuracy: {}".format(directed))

    best_acc = 0.

    if args.mode == "supervised_wpos":
        print("set DMV paramters directly")
        with torch.no_grad():
            model.set_dmv_params(train_data)

    print("begin training")
    batch_flag = False

    for epoch in range(args.epochs):
        report_ll = report_num_sents = report_num_words = 0
        if args.mode == "supervised_wopos":
            prior_optimizer.zero_grad()
            proj_optimizer.zero_grad()
            for cnt, i in enumerate(np.random.permutation(len(train_data.trees))):
                if batch_flag:
                    sub_iter = 0
                    for sub_cnt, sub_id in enumerate(np.random.permutation(len(train_data.trees))):
                        train_tree, num_words = train_data.trees[sub_id].tree, train_data.trees[sub_id].length
                        nll, jacobian_loss = model.supervised_loss_wopos(train_tree)
                        nll.backward()

                        if (sub_cnt+1) % args.batch_size == 0:
                            prior_optimizer.step()

                            prior_optimizer.zero_grad()
                            proj_optimizer.zero_grad()
                            sub_iter += 1
                            if sub_iter > 10:
                                batch_flag = False
                                break

                train_tree, embed = train_data.trees[i], train_data.embed[i]
                nll, jacobian_loss = model.supervised_loss_wopos(train_tree, embed)
                nll.backward()

                if (cnt+1) % args.batch_size == 0:
                    torch.nn.utils.clip_grad_norm_(model.proj_group, 5.0)
                    prior_optimizer.step()
                    proj_optimizer.step()

                    prior_optimizer.zero_grad()
                    proj_optimizer.zero_grad()
                    # batch_flag = True


                report_ll -= nll.item()
                report_num_words += num_words
                report_num_sents += 1

                if cnt % (log_niter * args.batch_size) == 0:
                    print('epoch %d, sent %d, ll_per_sent %.4f, ll_per_word %.4f, ' \
                          'max_var %.4f, min_var %.4f time elapsed %.2f sec' % \
                          (epoch, cnt, report_ll / report_num_sents, \
                          report_ll / report_num_words, model.var.data.max(), \
                          model.var.data.min(), time.time() - begin_time), file=sys.stderr)

        else:
            for iter_obj in train_data.data_iter(batch_size=args.batch_size):
                _, batch_size = iter_obj.pos.size()
                num_words = iter_obj.mask.sum().item()
                prior_optimizer.zero_grad()
                proj_optimizer.zero_grad()

                sents, jacobian_loss = model.transform(iter_obj.embed)
                sents = sents.transpose(0, 1)

                if args.mode == "unsupervised":
                    nll = model.unsupervised_loss(sents, iter_obj.masks)
                elif args.mode == "supervised_wpos":
                    nll = model.supervised_loss_wpos(iter_obj)
                else:
                    raise ValueError("{} mode is not supported".format(args.mode))

                avg_ll_loss = (nll + jacobian_loss) / batch_size

                avg_ll_loss.backward()

                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                proj_optimizer.step()

                report_ll -= nll.item()
                report_num_words += num_words
                report_num_sents += batch_size


                if train_iter % log_niter == 0:
                    print('epoch %d, iter %d, ll_per_sent %.4f, ll_per_word %.4f, ' \
                          'max_var %.4f, min_var %.4f time elapsed %.2f sec' % \
                          (epoch, train_iter, report_ll / report_num_sents, \
                          report_ll / report_num_words, model.var.data.max(), \
                          model.var.data.min(), time.time() - begin_time), file=sys.stderr)

                # break

                train_iter += 1

        print("\nTRAIN epoch {}: ll_per_sent: {:.4f}, ll_per_word: {:.4f}\n".format(
            epoch, report_ll / report_num_sents, report_ll / report_num_words))

        if args.mode == "supervised_wpos" or args.mode == "supervised_wopos":
            with torch.no_grad():
                acc = model.test(val_data)
                print('\nDEV: *****epoch {}, iter {}, acc {}*****\n'.format(
                    epoch, train_iter, acc))

            if acc > opt_dict["best_score"]:
                opt_dict["best_score"] = acc
                opt_dict["not_improved"] = 0
                torch.save(model.state_dict(), args.save_path)
            else:
                opt_dict["not_improved"] += 1
                if opt_dict["not_improved"] >= 5:
                    opt_dict["best_score"] = acc
                    opt_dict["not_improved"] = 0
                    opt_dict["lr"] = opt_dict["lr"] * lr_decay
                    model.load_state_dict(torch.load(args.save_path))
                    print("new lr decay: {}".format(opt_dict["lr"]))
                    if args.opt == "adam":
                        prior_optimizer = torch.optim.Adam(model.prior_group, lr=opt_dict["lr"] * args.prior_lr)
                        proj_optimizer = torch.optim.Adam(model.proj_group, lr=opt_dict["lr"] * args.proj_lr)
                    elif args.opt == "sgd":
                        prior_optimizer = torch.optim.SGD(model.prior_group, lr=opt_dict["lr"] * args.prior_lr)
                        proj_optimizer = torch.optim.SGD(model. proj_group, lr=opt_dict["lr"] * args.proj_lr)
        else:
            torch.save(model.state_dict(), args.save_path)

    torch.save(model.state_dict(), args.save_path)

if __name__ == '__main__':
    parse_args = init_config()
    main(parse_args)
