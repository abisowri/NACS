# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import matplotlib
matplotlib.use('Agg')

import aisweeper3
import time
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import random
import os
import numpy as np
import logging
import visdom
from torchtext import data, datasets
from collections import defaultdict
from itertools import count
import shutil

from seq2seq.nn.translationdataset import FactoredTranslationDataset
from seq2seq.test import predict, predict_and_save, predict_single_batch
from seq2seq.evaluation import evaluate, evaluate_bleu_external, \
    evaluate_exact_match, evaluate_bleu, evaluate_all, get_symbol_type_cooc_matrix, predict_and_get_bleu
from seq2seq.utils import save_checkpoint, print_examples, build_model, \
    print_parameter_info, time_since, as_minutes, get_state_dict, visdom_plot, get_random_examples, plot_examples, \
    animate_images, get_fields, plot_single_point_simple, plot_visdom_heatmap_simple, plot_heatmap_simple
from seq2seq.utils import EOS_TOKEN, PAD_TOKEN, UNK_TOKEN
from seq2seq.statistics import Statistics

logger = logging.getLogger(__name__)
use_cuda = torch.cuda.is_available()

SLURM_JOB_ID = os.environ['SLURM_JOB_ID'] if 'SLURM_JOB_ID' in os.environ else None
SLURM_JOB_NAME = os.environ['SLURM_JOB_NAME'] if 'SLURM_JOB_NAME' in os.environ else None
AISWEEPER_JOB_ID = os.environ['AISWEEPER_JOB_ID'] if 'AISWEEPER_JOB_ID' in os.environ else None

SLURM_JOB_NODELIST = os.environ['SLURM_JOB_NODELIST'] if 'SLURM_JOB_NODELIST' in os.environ else None
SLURM_NODEID = os.environ['SLURM_NODEID'] if 'SLURM_NODEID' in os.environ else None


def train_loop(model_type=None, enc_type=None, dec_type=None,
               src=None, trg=None, root=None, train=None, validation=None, test=None,
               src_tags='', trg_tags='', workdir=None,
               emb_dim=0, dim=0, dropout=0., word_dropout=0., weight_decay=0.,
               learning_rate=0., learning_rate_decay=1.,
               batch_size=1, n_iters=10000,
               save_every=0, print_every=0, plot_every=0, eval_every=0, tf_ratio=1.,
               resume="", max_length=0, max_length_train=0, seed=0, clip=5., metric="", emb_dim_tags=0, optimizer='adam',
               n_enc_layers=1, n_dec_layers=1, n_val_examples=5, use_visdom=False, mtl=False,
               coeff_ce=0., coeff_rl=0., coeff_rl_baseline=0., coeff_entropy=0., n_symbols=0,
               reward_type='logprob', ctx=True, context_start_iter=0,
               save_heatmaps=False,
               save_heatmap_animations=False, unk_src=True, unk_trg=True, eval_random_sym=False, eval_argmin_sym=False,
               pointer=False, ctx_dropout=0., ctx_dim=0, ctx_gate=False, ctx_detach=False,
               use_prev_word=True, use_dec_state=True, use_gold_symbols=False, use_ctx=True,
               gumbel_tau=1., gumbel_tau_decay=1.0, gumbel_tau_decay_steps=1000, gumbel_hard=True,
               predict_word_separately=False, num_composed_commands=0, rnn_type='gru',
               freeze_symbol=False, entropy_decay=1., entropy_decay_steps=-10,
               external_bleu=False, debpe=False, min_freq=0, symbol_word_gate=False, scan_normalize=False,
               pass_hidden_state=True,
               predict_from_emb=False, predict_from_ctx=False, predict_from_dec=False,
               dec_input_emb=False, dec_input_ctx=False,
               **kwargs):

    # Warning: data iterator stops reading input when an empty sequence is encountered on either side.
    if SLURM_JOB_NAME:
        workdir = os.path.join(workdir, SLURM_JOB_NAME)

    if AISWEEPER_JOB_ID:
        workdir = os.path.join(workdir, AISWEEPER_JOB_ID)

    if SLURM_JOB_NODELIST:
        logger.warning(SLURM_JOB_NODELIST)

    if SLURM_NODEID:
        logger.warning(SLURM_NODEID)

    # this is to assist sweeping with different training sets within the same sweep
    # the number of repetitions and the seed are linked
    # the number of composed jump commands is provided using an argument
    if num_composed_commands > -1:

        train = train.replace('num01', 'num%02d' % num_composed_commands)
        # train = train.replace('rep1', 'rep%d' % seed)
        validation = [x.replace('num01', 'num%02d' % num_composed_commands) for x in validation]
        # validation = [x.replace('rep1', 'rep%d' % seed) for x in validation]
        print(train)
        print(validation)
        print(test)

    cfg = {k: v for k, v in locals().items() if k != 'kwargs'}  # save all arguments to disk for resuming/testing

    logger.warning('Changed workdir to %s' % workdir)

    # set random seeds
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    random.seed(seed)
    np.random.seed(seed)
    device = None

    if not os.path.exists(workdir):
        os.makedirs(workdir)

    task_title = "_".join(root.split("/")[-2:])

    # input/output tags
    use_src_tags = True if mtl and (src_tags or emb_dim_tags > 0) else False
    use_trg_tags = True if mtl and trg_tags else False
    logger.info("src tags: %s" % use_src_tags)
    logger.info("trg tags: %s" % use_trg_tags)

    if not mtl:
        src_tags = ''
        trg_tags = ''

    factored_input = True if src_tags and emb_dim_tags > 0 else False
    predict_src_tags = True if mtl and src_tags and emb_dim_tags == 0 else False
    predict_trg_tags = True if mtl and trg_tags and model_type == "encdec" else False

    logger.info("factored input: %s" % factored_input)
    logger.info("predicting src tags: %s" % predict_src_tags)
    logger.info("predicting trg tags: %s" % predict_trg_tags)

    viz = visdom.Visdom() if use_visdom else None

    sos_src = True if enc_type == 'transformer' else False
    sos_trg = True if dec_type == 'transformer' else False

    # fields is always ordered as: src trg [src_tags] [trg_tags]
    fields, exts = get_fields(src=src, trg=trg, src_tags=src_tags, trg_tags=trg_tags, unk_src=unk_src, unk_trg=unk_trg,
                              sos_src=sos_src, sos_trg=sos_trg)

    src_field = fields[0][1]
    trg_field = fields[1][1]
    src_tags_field = fields[2][1] if use_src_tags else None
    trg_tags_field = fields[-1][1] if use_trg_tags else None

    val1 = validation[0] if len(validation) > 0 else None
    val2 = validation[1] if len(validation) > 1 else None

    # data sets - do not include trg_tags in valid/test
    train_data = FactoredTranslationDataset(os.path.join(root, train), exts=exts, fields=fields,
                                            max_length=max_length_train)
    # lim = 3 if use_src_tags else 2  # do not use trg tags for valid/test
    lim = len(exts)
    val_data = FactoredTranslationDataset(os.path.join(root, val1), exts=exts[:lim], fields=fields[:lim]) \
        if val1 is not None else None
    val2_data = FactoredTranslationDataset(os.path.join(root, val2), exts=exts[:lim], fields=fields[:lim]) \
        if val2 is not None else None
    test_data = FactoredTranslationDataset(os.path.join(root, test), exts=exts[:lim], fields=fields[:lim]) \
        if test is not None else None

    logger.info("Train data size: %d" % len(train_data))

    if val_data is not None:
        logger.info("Validation data size: %d" % len(val_data))

    if val2_data is not None:
        logger.info("Validation (2) data size: %d" % len(val2_data))

    if test_data is not None:
        logger.info("Test data size: %d" % len(test_data))

    # build vocabulary
    src_field.build_vocab(train_data.src, min_freq=min_freq)
    trg_field.build_vocab(train_data.trg, min_freq=min_freq)

    if use_src_tags:
        src_tags_field.build_vocab(train_data.src_tags)

    if use_trg_tags:
        trg_tags_field.build_vocab(train_data.trg_tags)

    n_tags_src = len(src_tags_field.vocab) if use_src_tags else 0
    n_tags_trg = len(trg_tags_field.vocab) if use_trg_tags else 0
    pad_idx_src = src_field.vocab.stoi[PAD_TOKEN]
    pad_idx_trg = trg_field.vocab.stoi[PAD_TOKEN]
    eos_idx_src = src_field.vocab.stoi[PAD_TOKEN]
    eos_idx_trg = trg_field.vocab.stoi[EOS_TOKEN]

    # this returns padded batches of (almost) equally sized inputs
    train_iter = data.BucketIterator(train_data, batch_size=batch_size, train=True, sort_within_batch=True, sort_key=lambda x: len(x.src), device=device, shuffle=True)

    val_iter = data.BucketIterator(val_data, batch_size=64, train=False, sort_within_batch=True, sort=True,
                                   shuffle=False, sort_key=lambda x: len(x.src), device=device)
    val2_iter = data.BucketIterator(val2_data, batch_size=64, train=False, sort_within_batch=True, sort=True,
                                    shuffle=False, sort_key=lambda x: len(x.src), device=device) if val2_data is not None else None
    test_iter = data.BucketIterator(test_data, batch_size=64, train=False, sort_within_batch=True, sort=True,
                                    shuffle=False, sort_key=lambda x: len(x.src), device=device) if test_data is not None else None

    val_iters = [val_iter]
    val_datas = [val_data]

    if val2_iter is not None:
        val_iters.append(val2_iter)
        val_datas.append(val2_data)

    # print vocabulary info
    for field_name, field in fields:
        logger.info("%s vocabulary size: %d" % (field_name, len(field.vocab)))
        logger.info("%s most common words: %s" % (
            field_name, " ".join([("%s (%d)" % x) for x in field.vocab.freqs.most_common(15)])))
        # for i, s in enumerate(field.vocab.itos):
        #     print(i, s)

    # print some examples
    train_examples = get_random_examples(next(iter(train_iter)), fields)
    val_examples = get_random_examples(next(iter(val_iter)), fields)
    val2_examples = get_random_examples(next(iter(val2_iter)), fields) if val2_iter is not None else None
    print_examples(train_examples, n=n_val_examples, msg="Train example")
    print_examples(val_examples, n=n_val_examples, msg="Validation example", start="")
    print_examples(val2_examples, n=n_val_examples, msg="Validation #2 example", start="")

    # save vocabularies
    if not resume:
        for field_name, field in fields:
            torch.save(field, os.path.join(workdir, field_name + ".pt.tar"))
            torch.save(field.vocab, os.path.join(workdir, field_name + "_vocab.pt.tar"))

    iters_per_epoch = int(np.ceil(len(train_data) / batch_size))
    logger.info("1 Epoch is approx. %d updates" % iters_per_epoch)

    # set the frequency to 1 epoch if *_every is -1,
    if save_every == -1:
        logger.info("Saving model every %d iters (~1 epoch)" % iters_per_epoch)
        save_every = iters_per_epoch

    if entropy_decay_steps < 0:

        logger.info("Entropy decay every %d iters (~%d epoch(s))" % (
            iters_per_epoch * -1 * entropy_decay_steps, -1 * entropy_decay_steps))

        entropy_decay_steps = iters_per_epoch * -1 * entropy_decay_steps
        cfg['entropy_decay_steps'] = entropy_decay_steps

    if eval_every == -1:
        logger.info("Evaluating every %d iters (~1 epoch)" % iters_per_epoch)
        eval_every = iters_per_epoch

    if gumbel_tau_decay_steps == -1:
        logger.info("Setting gumbel temperature decay to %d iters (~1 epoch)" % iters_per_epoch)
        gumbel_tau_decay_steps = iters_per_epoch + 1

    if n_iters < 0:
        n_epochs = -1 * n_iters
        n_iters = iters_per_epoch * n_epochs
        logger.info("Training for %d epochs (%d iters)" % (n_epochs, n_iters))

    # model creation
    model = build_model(n_words_src=len(src_field.vocab), n_words_trg=len(trg_field.vocab),
                        predict_src_tags=predict_src_tags, predict_trg_tags=predict_trg_tags,
                        factored_input=factored_input,
                        vocab_src=src_field.vocab, vocab_trg=trg_field.vocab,
                        n_tags_src=n_tags_src, n_tags_trg=n_tags_trg,
                        vocab_tags_src=src_tags_field.vocab if src_tags_field is not None else None,
                        vocab_tags_trg=trg_tags_field.vocab if trg_tags_field is not None else None,
                        **cfg)

    # statistics to keep track of during training (e.g. evaluation metrics)
    start_iter = 1
    train_stats = Statistics(name="train")
    valid_stats_list = [Statistics(name="val1", metric=metric)]
    if val2_data is not None:
        valid_stats_list.append(Statistics(name="val2", metric=metric))
    visdom_windows = defaultdict(lambda: None)
    cooc_matrices = defaultdict(lambda: np.zeros([n_symbols, len(trg_field.vocab)]))
    cooc_diffs = defaultdict(lambda: float)

    # simple uniform initialization
    # logger.warning("Using uniform initialization for ** everything **")
    # for p in model.parameters():
    #     p.data.uniform_(-0.04, 0.04)

    # use Xavier initialization for Transformer models
    if enc_type == 'transformer':
        logger.info('Using Xavier/Glorot uniform initialization')
        for p in model.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform(p)

    # set optimizer
    if optimizer == 'adam':
        opt = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    elif optimizer == 'sgd':
        opt = optim.SGD(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    else:
        raise ValueError('Unknown optimizer %s' % optimizer)

    if optimizer == 'sgd':

        def lr_lambda(x):
            return learning_rate_decay ** (x // iters_per_epoch)

        scheduler = optim.lr_scheduler.LambdaLR(opt, lr_lambda=lr_lambda)

    else:
        scheduler = optim.lr_scheduler.StepLR(opt, iters_per_epoch, gamma=1.0)

    # optionally resume from a checkpoint
    if resume:
        if os.path.isfile(resume):
            logger.info("Loading checkpoint '{}'".format(resume))
            checkpoint = torch.load(resume)
            # cfg = checkpoint['cfg']
            start_iter = checkpoint['iter'] + 1
            model.load_state_dict(checkpoint['state_dict'])
            opt.load_state_dict(checkpoint['opt'])
            valid_stats_list[0].best = checkpoint['best_eval_score']
            valid_stats_list[0].best_iter = checkpoint['best_eval_iter']
            logger.info("Loaded checkpoint '{}' (iter {})".format(resume, checkpoint['iter']))
        else:
            logger.warning("No checkpoint found at '{}'".format(resume))
            exit()

    print_parameter_info(model)

    criterion = nn.NLLLoss(reduce=True, size_average=False, ignore_index=pad_idx_trg)
    src_tag_criterion = nn.NLLLoss(reduce=True, size_average=False, ignore_index=0)
    trg_tag_criterion = nn.NLLLoss(reduce=True, size_average=False, ignore_index=0)
    actual_train_iter = iter(train_iter)

    logger.info("Training starts...")
    start = time.time()
    iter_i = start_iter

    # main training loop: perform n_iters updates
    for iter_i in range(start_iter, n_iters + 1):

        scheduler.step()

        examples_seen = (iter_i - 1) * batch_size
        epoch = examples_seen // len(train_data) + 1
        batch = next(actual_train_iter)
        loss_dict, predictions, src_tag_predictions, trg_tag_predictions, trg_symbols = train_on_minibatch(
            batch, model, opt, criterion, clip=clip, src_vocab=src_field.vocab,
            trg_vocab=trg_field.vocab, tf_ratio=tf_ratio,
            predict_src_tags=predict_src_tags,
            predict_trg_tags=predict_trg_tags,
            iter_i=iter_i, max_length_train=max_length_train)

        loss = loss_dict['loss']
        train_stats.acc_loss += loss

        # print info
        if iter_i % print_every == 0:
            lr = scheduler.get_lr()[0]
            logger.info('Epoch %d Iter %08d (%d%%) Time %s Loss %.4f Lr %.6f' % (
                epoch, iter_i, iter_i / n_iters * 100, time_since(start, iter_i / n_iters), loss, lr))
            logger.info(" ".join("%s=%f" % (k, v) for k, v in loss_dict.items()))

            if use_visdom:
                for k, v in loss_dict.items():
                    plot_single_point_simple(iter_i, loss_dict[k], metric=k,
                                             visdom_windows=visdom_windows, title='tr_'+k)
                plot_single_point_simple(iter_i, lr, metric='lr', visdom_windows=visdom_windows, title='lr')

                if 'coeff_entropy' in loss_dict:
                    plot_single_point_simple(iter_i, loss_dict['coeff_entropy'], metric='coeff_entropy',
                                             visdom_windows=visdom_windows, title='coeff_entropy')

        # evaluate
        if iter_i % eval_every == 0:
            if validation is not None:

                train_stats.eval_iters.append(iter_i)

                # save train loss since last evaluation
                train_stats.loss.append(train_stats.acc_loss)
                train_stats.acc_loss = 0.

                # print train examples
                train_examples = get_random_examples(batch, fields, predictions=predictions,
                                                     src_tag_predictions=src_tag_predictions,
                                                     trg_tag_predictions=trg_tag_predictions,
                                                     trg_symbols=trg_symbols,
                                                     n=n_val_examples)
                print_examples(train_examples, msg="Train example", n=n_val_examples)

                if use_visdom:
                    visdom_plot(stats=train_stats, eval_every=eval_every,
                                visdom_windows=visdom_windows, title=task_title)

                logger.info("Evaluation starts.. @ iter %d" % iter_i)
                log_dict = {'iter': iter_i}

                if 'coeff_entropy' in loss_dict:
                    log_dict['coeff_entropy'] = loss_dict['coeff_entropy']

                # evaluate on each validation set
                for val_id, val_it, val_data, val_stats in zip(count(start=1), val_iters, val_datas, valid_stats_list):

                    val_stats.eval_iters.append(iter_i)

                    # print validation examples
                    example_iter = data.Iterator(dataset=val_data, batch_size=128, train=False, sort=False,
                                                 sort_within_batch=True, shuffle=True, sort_key=lambda x: len(x.src),
                                                 device=device)
                    example_batch = next(iter(example_iter))
                    result = predict_single_batch(model, example_batch, max_length=max_length, return_attention=True,
                                                  predict_src_tags=predict_src_tags, predict_trg_tags=predict_trg_tags)
                    predictions = result['preds']
                    attention_scores = result['att_scores'] if 'att_scores' in result else None
                    src_tag_preds = result['src_tag_preds'] if 'src_tag_preds' in result else None
                    trg_tag_preds = result['trg_tag_preds'] if 'trg_tag_preds' in result else None
                    trg_symbols = result['symbols'] if 'symbols' in result else None
                    result = None

                    valid_examples = get_random_examples(example_batch, fields,
                                                         predictions=predictions,
                                                         src_tag_predictions=src_tag_preds,
                                                         trg_tag_predictions=trg_tag_preds,
                                                         trg_symbols=trg_symbols,
                                                         attention_scores=attention_scores, n=n_val_examples, seed=42)
                    print_examples(valid_examples, msg="Val #%d example" % val_id, n=n_val_examples)

                    acc, ppx, em, bleu = evaluate_all(
                        model=model, batch_iter=val_it,
                        src_vocab=src_field.vocab, trg_vocab=trg_field.vocab,
                        src_vocab_tags=src_tags_field.vocab if src_tags_field is not None else None,
                        trg_vocab_tags=trg_tags_field.vocab if trg_tags_field is not None else None,
                        max_length=max_length, scan_normalize=scan_normalize)

                    # predict and save to disk
                    if external_bleu:
                        logger.info("Getting external BLEU score, val%d" % val_id)
                        output_path = os.path.join(workdir, "output.val%d.iter%08d.%s" % (val_id, iter_i, trg))
                        trg_path = os.path.join(root, "%s.%s" % (validation[val_id-1], trg))
                        bleu = predict_and_get_bleu(dataset=val_data, model=model, output_path=output_path,
                                                              max_length=max_length, device=device, trg_path=trg_path,
                                                              src_vocab=src_field.vocab, trg_vocab=trg_field.vocab,
                                                              debpe=debpe)
                        logger.info("Val%d multi-bleu: %f" % (val_id, bleu))

                    is_best = val_stats.add(acc, ppx, em, bleu, iter_i)

                    # save results for logger
                    log_dict.update({val_stats.name + '_acc': acc, val_stats.name + '_ppx': ppx,
                                     val_stats.name + '_em': em, val_stats.name + '_bleu': bleu})

                    # save best model
                    if is_best:
                        state = get_state_dict(iter_i, model_type, model.state_dict(),
                                               opt.state_dict(), valid_stats_list)
                        filename = 'checkpoint.best.val%d.pt.tar' % val_id
                        save_path = os.path.join(workdir, filename)
                        save_checkpoint(state, save_path)

                        if external_bleu:
                            best_path = os.path.join(workdir, "output.val%08d.best.%s" % (val_id, trg))
                            shutil.copy2(output_path, best_path)
                            if debpe:
                                shutil.copy2(output_path + '.debpe', best_path + '.debpe')
                        logger.info("Saved best model for valid %d at iter %d" % (val_id, iter_i))

                    # co-occurrence matrix
                    if model_type == 'model1':
                        cooc = get_symbol_type_cooc_matrix(model=model, batch_iter=val_it, trg_vocab=trg_field.vocab,
                                                           max_length=max_length, n_symbols=n_symbols)
                        cooc_word_totals = cooc.sum(0) + 1e-8
                        cooc_norm = cooc / cooc_word_totals

                        old = cooc_matrices['val%d' % val_id]
                        cooc_diff = np.sum(np.abs(cooc_norm-old))
                        cooc_diffs['val%d' % val_id] = cooc_diff
                        cooc_matrices['val%d' % val_id] = cooc_norm
                        log_dict['cooc_diff_val%d' % val_id] = cooc_diff
                        logger.info("Co-ocurrence Val #%d Iter %d Difference %f" % (val_id, iter_i, cooc_diff))

                        if use_visdom:
                            plot_single_point_simple(iter_i, cooc_diff, metric='cooc_diff_val%d' % val_id,
                                                     visdom_windows=visdom_windows, title='cooc_diff_val%d' % val_id)

                        logger.info("Saved co-occurrence Val #%d Iter %d" % (val_id, iter_i))
                        cooc_path = os.path.join(workdir, 'cooc_iter%08d_val%d.npz' % (iter_i, val_id))
                        np.savez(cooc_path, cooc)

                        if use_visdom:
                            logger.info("Plotting heatmaps in Visdom")
                            rownames = ["S%d" % d for d in range(n_symbols)]
                            columnnames = [trg_field.vocab.itos[x] for x in range(len(trg_field.vocab))]
                            cooc_title = "Co-occurrence Val #%d Iter %d" % (val_id, iter_i)
                            plot_visdom_heatmap_simple(cooc_norm, title=cooc_title,
                                                       columnnames=columnnames, rownames=rownames, colormap='Viridis')
                        if save_heatmaps:
                            logger.info("Saving heatmaps")
                            rownames = ["S%d" % d for d in range(n_symbols)]
                            columnnames = [trg_field.vocab.itos[x] for x in range(len(trg_field.vocab))]
                            cooc_path = os.path.join(workdir, 'cooc_iter%08d_val%d.png' % (iter_i, val_id))
                            plot_heatmap_simple(cooc_norm, path=cooc_path, columnnames=columnnames, rownames=rownames)

                        # print L2-norm per symbol
                        l2_norm_per_symbol = (model.decoder.symbol_embedding.weight ** 2).sum(-1).sqrt()
                        l2_norm_per_symbol = l2_norm_per_symbol.data.view(-1).tolist()
                        log_dict.update({'sym%d_l2' % k: v for k, v in enumerate(l2_norm_per_symbol)})
                        print("l2_norm_per_symbol: " + ", ".join([str(x) for x in l2_norm_per_symbol]))
                        if use_visdom:
                            for i, i_v in enumerate(l2_norm_per_symbol):
                                plot_single_point_simple(iter_i, i_v, metric='norm_symbol_%d' % i,
                                                         visdom_windows=visdom_windows, title='norm_symbol_%d' % i)

                    # plot
                    if use_visdom:
                        visdom_plot(stats=val_stats, eval_every=eval_every, visdom_windows=visdom_windows,
                                    title=task_title)

                    plot_examples(valid_examples, iteration=iter_i, workdir=workdir, n=10,
                                  plot_file_fmt=task_title + '_val%d' % val_id + '_iter%06d_example%03d',
                                  use_visdom=use_visdom, save_to_disk=save_heatmaps)

                    # heatmap animations
                    if save_heatmaps and save_heatmap_animations:
                        logger.info("Creating heatmap animations")
                        for i in range(1, n_val_examples + 1):
                            filenames = [task_title + '_val%d_iter%06d_example%03d.png' % (val_id, iteration, i) for iteration in
                                         val_stats.eval_iters]
                            filenames = [os.path.join(workdir, fname) for fname in filenames]
                            output_path = os.path.join(workdir, 'val%d_example%03d.gif' % (val_id, i))
                            animate_images(output_path=output_path, filenames=filenames)

                if test_iter is not None:
                    test_acc, test_ppx, test_exact_match, test_bleu = evaluate_all(
                        model=model, batch_iter=test_iter,
                        src_vocab=src_field.vocab, trg_vocab=trg_field.vocab,
                        src_vocab_tags=src_tags_field.vocab if src_tags_field is not None else None,
                        trg_vocab_tags=trg_tags_field.vocab if trg_tags_field is not None else None,
                        max_length=max_length, scan_normalize=scan_normalize)

                    # test set - predict and save to disk
                    if external_bleu:
                        logger.info("Getting external test BLEU")
                        output_path = os.path.join(workdir, "output.test.iter%08d.%s" % (iter_i, trg))
                        trg_path = os.path.join(root, "%s.%s" % (test, trg))
                        test_bleu = predict_and_get_bleu(dataset=test_data, model=model, output_path=output_path,
                                                              max_length=max_length, device=device, trg_path=trg_path,
                                                              src_vocab=src_field.vocab, trg_vocab=trg_field.vocab,
                                                              debpe=debpe)
                        logger.info("Test multi-bleu: %f" % test_bleu)

                    log_dict['test_acc'] = test_acc
                    log_dict['test_ppx'] = test_ppx
                    log_dict['test_em'] = test_exact_match
                    log_dict['test_bleu'] = test_bleu

                # any additional evaluations
                if model_type == 'model1' and eval_random_sym:

                    model.eval_random_sym = True

                    for val_id, val_it, val_data, val_stats in zip(count(start=1), val_iters, val_datas, valid_stats_list):
                        for feed_original_sym in [True]:

                            model.feed_original_sym = feed_original_sym
                            acc, ppx, em, bleu = evaluate_all(
                                model=model, batch_iter=val_it,
                                src_vocab=src_field.vocab, trg_vocab=trg_field.vocab,
                                src_vocab_tags=src_tags_field.vocab if src_tags_field is not None else None,
                                trg_vocab_tags=trg_tags_field.vocab if trg_tags_field is not None else None,
                                max_length=max_length, scan_normalize=scan_normalize)

                            infix = '_symrand' if feed_original_sym else '_symrand_feedrand'
                            log_dict.update({'val%d%s_acc' % (val_id, infix): acc,
                                             'val%d%s_ppx' % (val_id, infix): ppx,
                                             'val%d%s_em' % (val_id, infix): em,
                                             'val%d%s_bleu' % (val_id, infix): bleu})

                    model.eval_random_sym = False

                if model_type == 'model1' and eval_argmin_sym:

                    model.eval_argmin_sym = True
                    for val_id, val_it, val_data, val_stats in zip(count(start=1), val_iters, val_datas, valid_stats_list):
                        for feed_original_sym in [True]:

                            model.feed_original_sym = feed_original_sym
                            acc, ppx, em, bleu = evaluate_all(
                                model=model, batch_iter=val_it,
                                src_vocab=src_field.vocab, trg_vocab=trg_field.vocab,
                                src_vocab_tags=src_tags_field.vocab if src_tags_field is not None else None,
                                trg_vocab_tags=trg_tags_field.vocab if trg_tags_field is not None else None,
                                max_length=max_length, scan_normalize=scan_normalize)

                            infix = '_symmin' if feed_original_sym else '_symmin_feedmin'
                            log_dict.update({'val%d%s_acc' % (val_id, infix): acc,
                                             'val%d%s_ppx' % (val_id, infix): ppx,
                                             'val%d%s_em' % (val_id, infix): em,
                                             'val%d%s_bleu' % (val_id, infix): bleu})

                    model.eval_argmin_sym = False

                aisweeper3.log(log_dict, cfg)

        # save checkpoint
        # if iter_i % save_every == 0:
        #     state = get_state_dict(iter_i, model_type, model.state_dict(), opt.state_dict(), valid_stats_list)
        #     filename = 'checkpoint.iter%08d.pt.tar' % iter_i
        #     save_path = os.path.join(workdir, filename)
        #     save_checkpoint(state, save_path)
        #     logger.info("Saved checkpoint at iter %d" % iter_i)

    logger.info("Training finished\n-----------------")
    for val_id, val_stats in enumerate(valid_stats_list, 1):
        logger.info("Best validation #%d %s = %f @ iter %d, acc %.4f ppx %.4f em %.4f bleu %.4f" % (
            val_id, metric, val_stats.best, val_stats.best_iter,
            val_stats.best_acc, val_stats.best_ppx, val_stats.best_em, val_stats.best_bleu))

    # evaluate on test
    if test is not None:
        for val_id, val_it, val_data, val_stats in zip(count(start=1), val_iters, val_datas, valid_stats_list):

            # load best model according to this validation set
            logger.info("loading best model (for validation #%d)" % val_id)
            checkpoint = torch.load(os.path.join(workdir, 'checkpoint.best.val%d.pt.tar' % val_id))
            model.load_state_dict(checkpoint['state_dict'])
            logger.info("Best iter @ %d" % checkpoint['stats'][val_id-1].best_iter)

            # predict and save to disk
            output_path = os.path.join(workdir, "test.final.best.val%d.%s" % (val_id, trg))
            trg_path = os.path.join(root, "%s.%s" % (test, trg))
            ext_bleu_score = predict_and_get_bleu(dataset=test_data, model=model, output_path=output_path,
                                                  max_length=max_length, device=device, trg_path=trg_path,
                                                  src_vocab=src_field.vocab, trg_vocab=trg_field.vocab, debpe=True)
            logger.info("Test multi-bleu: %f" % ext_bleu_score)


def train_on_minibatch(batch, model, opt, criterion, clip=0., tf_ratio=1.,
                       src_vocab=None, trg_vocab=None, predict_src_tags=False, predict_trg_tags=False,
                       iter_i=0, max_length_train=0):
    """
    Get loss on one mini-batch

    Args:
        batch:
        model:
        opt: optimizer
        criterion:
        clip:
        tf_ratio: teacher forcing ratio
        src_vocab:
        trg_vocab:

    Returns:

    """

    # opt.zero_grad()
    model.train()

    src_var, src_lengths = batch.src
    trg_var, trg_lengths = batch.trg

    # FIXME
    # if max_length_train > 0:
    #     src_var = src_var[:, :max_length_train]
    #     trg_var = trg_var[:, :max_length_train]
    #     src_lengths = torch.clamp(src_lengths, 0, max_length_train)
    #     trg_lengths = torch.clamp(trg_lengths, 0, max_length_train)

    src_tags_var = batch.src_tags if hasattr(batch, 'src_tags') else None
    trg_tags_var = batch.trg_tags if hasattr(batch, 'trg_tags') else None

    # if src_tags_var is not None and max_length_train > 0:
    #     src_tags_var = src_tags_var[:, :max_length_train]
    #
    # if trg_tags_var is not None and max_length_train > 0:
    #     trg_tags_var = trg_tags_var[:, :max_length_train]

    batch_size = trg_var.size(0)
    time_steps = trg_var.size(1)

    src_lengths = src_lengths.view(-1).tolist()
    trg_lengths = trg_lengths.view(-1).tolist()

    max_length = trg_var.size(1)

    # forward pass
    result = model(src_var=src_var, src_lengths=src_lengths,
                   trg_var=trg_var, trg_lengths=trg_lengths,
                   max_length=max_length, tf_ratio=tf_ratio,
                   src_tags_var=src_tags_var,
                   trg_tags_var=trg_tags_var,
                   predict_src_tags=predict_src_tags,
                   predict_trg_tags=predict_trg_tags,
                   iter_i=iter_i)

    predictions = result['preds'].cpu()

    if src_tags_var is not None and predict_src_tags:
        src_tag_predictions = result['src_tag_preds'].data.cpu()
    else:
        src_tag_predictions = None

    if 'trg_tag_log_probs' in result:
        trg_tag_predictions = result['trg_tag_preds'].data.cpu()
    else:
        trg_tag_predictions = None

    loss_dict = result['loss']
    loss = loss_dict['loss']

    if 'symbols' in result:
        trg_symbols = result['symbols']
    else:
        trg_symbols = None

    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
    opt.step()
    opt.zero_grad()

    # loss_dict_values = {k: v.data.cpu().tolist()[0] for k, v in loss_dict.items()}
    loss_dict_values = {k: v.data.cpu().tolist() for k, v in loss_dict.items()}

    return loss_dict_values, predictions, src_tag_predictions, trg_tag_predictions, trg_symbols