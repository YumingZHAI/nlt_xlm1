# Copyright (c) 2019-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from logging import getLogger
import os
import copy
import time
import json
from collections import OrderedDict

import torch
from torch import nn
import torch.nn.functional as F
import sys

# .. means in the parent directory
from ..optim import get_optimizer
from ..utils import concat_batches, truncate, to_cuda
from ..data.dataset import ParallelDataset
from ..data.loader import load_binarized, set_dico_parameters


# XNLI_LANGS = ['ar', 'bg', 'de', 'el', 'en', 'es', 'fr', 'hi', 'ru', 'sw', 'th', 'tr', 'ur', 'vi', 'zh']

XNLI_LANGS = ['en', 'fr', 'zh']

# modify the logger here, log both to a file and to the console
logger = getLogger()

class XNLI:

    def __init__(self, embedder, scores, params):
        """
        Initialize XNLI trainer / evaluator.
        Initial `embedder` should be on CPU to save memory.
        """
        self._embedder = embedder
        self.params = params
        self.scores = scores

    def get_iterator(self, splt, lang):
        """
        Get a monolingual data iterator.
        """
        assert splt in ['valid', 'test'] or splt == 'train' and lang == 'en'
        # TODO understand this method in class ParallelDataset
        # only shuffle the training dataset
        return self.data[lang][splt]['x'].get_iterator(
            shuffle=(splt == 'train'),
            group_by_size=self.params.group_by_size,
            return_indices=True
        )

    def run(self):
        """
        Run XNLI training / evaluation.
        """
        params = self.params

        # load data
        self.data = self.load_data()

        if not self.data['dico'] == self._embedder.dico:
            raise Exception(("Dictionary in evaluation data (%i words) seems different than the one " +
                             "in the pretrained model (%i words). Please verify you used the same dictionary, " +
                             "and the same values for max_vocab and min_count.") % (len(self.data['dico']), len(self._embedder.dico)))

        # embedder
        self.embedder = copy.deepcopy(self._embedder)
        # disable using cuda here
        # self.embedder.cuda()

        # disable using cuda here
        # projection layer
        # self.proj = nn.Sequential(*[
        #     nn.Dropout(params.dropout),
        #     nn.Linear(self.embedder.out_dim, 3)
        # ]).cuda()

        # class torch.nn.Sequential(*args)   take variable arguments
        # in class SentenceEmbedder: self.out_dim = model.dim
        # predict into 3 output classes
        self.proj = nn.Sequential(*[
            nn.Dropout(params.dropout),
            nn.Linear(self.embedder.out_dim, 3)
        ])

        # optimizers (TODO later, check here in detail)
        self.optimizer_e = get_optimizer(list(self.embedder.get_parameters(params.finetune_layers)), params.optimizer_e)
        self.optimizer_p = get_optimizer(self.proj.parameters(), params.optimizer_p)

        # train and evaluate the model
        for epoch in range(params.n_epochs):
            # update epoch
            self.epoch = epoch
            # training
            logger.info("XNLI - Training epoch %i ..." % epoch)
            self.train()
            # evaluation
            logger.info("XNLI - Evaluating epoch %i ..." % epoch)
            # deactivate the autograd engine, don't backprop
            with torch.no_grad():
                scores = self.eval()
                self.scores.update(scores)

    def train(self):
        """
        Finetune for one epoch on the XNLI English training set.
        """
        params = self.params
        # these two train() method comes from nn.Module
        # first the XLM model and output projection layer
        self.embedder.train()
        self.proj.train()

        # training variables
        losses = []
        ns = 0  # number of sentences
        nw = 0  # number of words
        t = time.time()

        # iterator of the training dataset
        iterator = self.get_iterator('train', 'en')
        lang_id = params.lang2id['en']

        while True:
            # batch
            try:
                batch = next(iterator)
            except StopIteration:
                break
            # todo understand the data structure
            (sent1, len1), (sent2, len2), idx = batch
            # sent1: by column: each sentence, by line: each word piece id?
            sent1, len1 = truncate(sent1, len1, params.max_len, params.eos_index)
            sent2, len2 = truncate(sent2, len2, params.max_len, params.eos_index)
            x, lengths, positions, langs = concat_batches(
                sent1, len1, lang_id,
                sent2, len2, lang_id,
                params.pad_index,
                params.eos_index,
                reset_positions=False
            )
            # print(x)  # combine two sentences
            # print(langs) # en corresponds to 4
            y = self.data['en']['train']['y'][idx]
            bs = len(len1)   # batchsize

            # cuda
            # disable the cuda here
            # x, y, lengths, positions, langs = to_cuda(x, y, lengths, positions, langs)

            # loss (for each batch of 8 sentences)
            output = self.proj(self.embedder.get_embeddings(x, lengths, positions, langs))
            loss = F.cross_entropy(output, y)

            # backward / optimization
            # must first call .zero_grad()
            self.optimizer_e.zero_grad()
            self.optimizer_p.zero_grad()
            loss.backward()
            self.optimizer_e.step()
            self.optimizer_p.step()

            # update statistics
            ns += bs
            nw += lengths.sum().item()
            # print(type(lengths))  <class 'torch.Tensor'>
            losses.append(loss.item())

            # log
            # ns: multiple of 8. so every 100*bs batches, print the average loss
            if ns % (100 * bs) < bs:
                logger.info("XNLI - Epoch %i - Train iter %7i - %.1f words/s - Loss: %.4f" %
                            (self.epoch, ns, nw / (time.time() - t), sum(losses) / len(losses)))   # average loss
                nw, t = 0, time.time()
                losses = []

            # epoch size
            if params.epoch_size != -1 and ns >= params.epoch_size:
                break

    def eval(self):
        """
        Evaluate on XNLI validation and test sets, for all languages.
        """
        params = self.params
        self.embedder.eval()
        self.proj.eval()
        scores = OrderedDict({'epoch': self.epoch})

        for splt in ['valid', 'test']:
            for lang in XNLI_LANGS:
                # in glue-xnli.py: params.lang2id = embedder.pretrain_params['lang2id']
                if lang not in params.lang2id:
                    continue
                lang_id = params.lang2id[lang]
                valid = 0
                total = 0

                for batch in self.get_iterator(splt, lang):
                    (sent1, len1), (sent2, len2), idx = batch
                    x, lengths, positions, langs = concat_batches(
                        sent1, len1, lang_id,
                        sent2, len2, lang_id,
                        params.pad_index,
                        params.eos_index,
                        reset_positions=False
                    )
                    y = self.data[lang][splt]['y'][idx]
                    # disable cuda here
                    # x, y, lengths, positions, langs = to_cuda(x, y, lengths, positions, langs)
                    # forward
                    output = self.proj(self.embedder.get_embeddings(x, lengths, positions, langs))
                    # todo understand here after:
                    predictions = output.data.max(1)[1]
                    # update statistics
                    valid += predictions.eq(y).sum().item()
                    total += len(len1)
                # compute accuracy
                acc = 100.0 * valid / total
                scores['xnli_%s_%s_acc' % (splt, lang)] = acc
                logger.info("XNLI - %s - %s - Epoch %i - Acc: %.1f%%" % (splt, lang, self.epoch, acc))
        logger.info("__log__:%s" % json.dumps(scores))
        return scores

    def load_data(self):
        """
        Load XNLI cross-lingual classification data.
        """
        params = self.params
        # 392702 train.label.en   (size of dataset)
        # 5010 test.label.*
        # 2490 valid.label.*
        data = {lang: {splt: {} for splt in ['train', 'valid', 'test']} for lang in XNLI_LANGS}

        label2id = {'contradiction': 0, 'neutral': 1, 'entailment': 2}
        # params.data_path: ./data/processed/XLM15
        dpath = os.path.join(params.data_path, 'eval', 'XNLI')

        for splt in ['train', 'valid', 'test']:
            for lang in XNLI_LANGS:
                # only English has a training set
                if splt == 'train' and lang != 'en':
                    del data[lang]['train']
                    continue

                # load data and dictionary
                # TODO, need to understand the preprocessing steps
                data1 = load_binarized(os.path.join(dpath, '%s.s1.%s.pth' % (splt, lang)), params)
                data2 = load_binarized(os.path.join(dpath, '%s.s2.%s.pth' % (splt, lang)), params)
                data['dico'] = data.get('dico', data1['dico'])

                # set (or update) dictionary parameters
                set_dico_parameters(params, data, data1['dico'])
                set_dico_parameters(params, data, data2['dico'])

                # check evernote to understand data1['sentences'], data1['positions']
                # create dataset, e.g. data[en][train]['x'] = ParallelDataset(____)
                data[lang][splt]['x'] = ParallelDataset(
                    data1['sentences'], data1['positions'],
                    data2['sentences'], data2['positions'],
                    params
                )

                # load labels
                with open(os.path.join(dpath, '%s.label.%s' % (splt, lang)), 'r') as f:
                    labels = [label2id[l.rstrip()] for l in f]

                data[lang][splt]['y'] = torch.LongTensor(labels)
                # tensor([1, 2, 2,  ..., 2, 1, 1])

                assert len(data[lang][splt]['x']) == len(data[lang][splt]['y'])
        return data
