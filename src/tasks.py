'''Define the tasks and code for loading their data.

- As much as possible, following the existing task hierarchy structure.
- When inheriting, be sure to write and call load_data.
- Set all text data as an attribute, task.sentences (List[List[str]])
- Each task's val_metric should be name_metric, where metric is returned by
get_metrics(): e.g. if task.val_metric = task_name + "_accuracy", then
task.get_metrics() should return {"accuracy": accuracy_val, ... }
'''
import copy
import collections
import itertools
import os
import math
import logging as log
import json
import numpy as np
from typing import Iterable, Sequence, List, Dict, Any, Type
import torch

import allennlp.common.util as allennlp_util
from allennlp.training.metrics import CategoricalAccuracy, \
    BooleanAccuracy, F1Measure, Average
from allennlp.data.token_indexers import SingleIdTokenIndexer
from .allennlp_mods.correlation import Correlation, FastMatthews

# Fields for instance processing
from allennlp.data import Instance, Token
from allennlp.data.fields import TextField, LabelField, \
    SpanField, ListField, MetadataField
from .allennlp_mods.numeric_field import NumericField
from .allennlp_mods.multilabel_field import MultiLabelField

from . import serialize
from . import utils
from .utils import load_tsv, process_sentence, truncate, load_diagnostic_tsv
import codecs

UNK_TOK_ALLENNLP = "@@UNKNOWN@@"
UNK_TOK_ATOMIC = "UNKNOWN"  # an unk token that won't get split by tokenizers

REGISTRY = {}  # Do not edit manually!


def register_task(name, rel_path, **kw):
    '''Decorator to register a task.

    Use this instead of adding to NAME2INFO in preprocess.py

    If kw is not None, this will be passed as additional args when the Task is
    constructed in preprocess.py.

    Usage:
    @register_task('mytask', 'my-task/data', **extra_kw)
    class MyTask(SingleClassificationTask):
        ...
    '''
    def _wrap(cls):
        entry = (cls, rel_path, kw) if kw else (cls, rel_path)
        REGISTRY[name] = entry
        return cls
    return _wrap


def _sentence_to_text_field(sent: Sequence[str], indexers: Any):
    ''' Helper function to map a sequence of tokens into a sequence of
    AllenNLP Tokens, then wrap in a TextField with the given indexers '''
    return TextField(list(map(Token, sent)), token_indexers=indexers)


def _atomic_tokenize(sent: str, atomic_tok: str, nonatomic_toks: List[str], max_seq_len: int):
    ''' Replace tokens that will be split by tokenizer with a
    placeholder token. Tokenize, and then substitute the placeholder
    with the *first* nonatomic token in the list. '''
    for nonatomic_tok in nonatomic_toks:
        sent = sent.replace(nonatomic_tok, atomic_tok)
    sent = process_sentence(sent, max_seq_len)
    sent = [nonatomic_toks[0] if t == atomic_tok else t for t in sent]
    return sent


def process_single_pair_task_split(split, indexers, is_pair=True, classification=True):
    '''
    Convert a dataset of sentences into padded sequences of indices. Shared
    across several classes.

    Args:
        - split (list[list[str]]): list of inputs (possibly pair) and outputs
        - pair_input (int)
        - tok2idx (dict)

    Returns:
        - instances (list[Instance]): a list of AllenNLP Instances with fields
    '''
    def _make_instance(input1, input2, labels, idx):
        d = {}
        d["input1"] = _sentence_to_text_field(input1, indexers)
        d['sent1_str'] = MetadataField(" ".join(input1[1:-1]))
        if input2:
            d["input2"] = _sentence_to_text_field(input2, indexers)
            d['sent2_str'] = MetadataField(" ".join(input2[1:-1]))
        if classification:
            d["labels"] = LabelField(labels, label_namespace="labels",
                                     skip_indexing=True)
        else:
            d["labels"] = NumericField(labels)

        d["idx"] = LabelField(idx, label_namespace="idxs",
                              skip_indexing=True)

        return Instance(d)

    split = list(split)
    if not is_pair:  # dummy iterator for input2
        split[1] = itertools.repeat(None)
    if len(split) < 4:  # counting iterator for idx
        assert len(split) == 3
        split.append(itertools.count())

    # Map over columns: input2, (input2), labels, idx
    instances = map(_make_instance, *split)
    #  return list(instances)
    return instances  # lazy iterator


class Task():
    '''Generic class for a task

    Methods and attributes:
        - load_data: load dataset from a path and create splits
        - truncate: truncate data to be at most some length
        - get_metrics:

    Outside the task:
        - process: pad and indexify data given a mapping
        - optimizer
    '''

    def __init__(self, name):
        self.name = name

    def load_data(self, path, max_seq_len):
        ''' Load data from path and create splits. '''
        raise NotImplementedError

    def truncate(self, max_seq_len, sos_tok, eos_tok):
        ''' Shorten sentences to max_seq_len and add sos and eos tokens. '''
        raise NotImplementedError

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        yield from self.sentences

    def count_examples(self, splits=['train', 'val', 'test']):
        ''' Count examples in the dataset. '''
        self.example_counts = {}
        for split in splits:
            st = self.get_split_text(split)
            count = self.get_num_examples(st)
            self.example_counts[split] = count

    @property
    def tokenizer_name(self):
        ''' Get the name of the tokenizer used for this task.

        Generally, this is just MosesTokenizer, but other tokenizations may be
        needed in special cases such as when working with BPE-based models
        such as the OpenAI transformer LM.
        '''
        return utils.TOKENIZER.__class__.__name__

    @property
    def n_train_examples(self):
        return self.example_counts['train']

    @property
    def n_val_examples(self):
        return self.example_counts['val']

    def get_split_text(self, split: str):
        ''' Get split text, typically as list of columns.

        Split should be one of 'train', 'val', or 'test'.
        '''
        return getattr(self, '%s_data_text' % split)

    def get_num_examples(self, split_text):
        ''' Return number of examples in the result of get_split_text.

        Subclass can override this if data is not stored in column format.
        '''
        return len(split_text[0])

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        raise NotImplementedError

    def get_metrics(self, reset: bool=False) -> Dict:
        ''' Get metrics specific to the task. '''
        raise NotImplementedError


class ClassificationTask(Task):
    ''' General classification task '''

    def __init__(self, name):
        super().__init__(name)


class RegressionTask(Task):
    ''' General regression task '''

    def __init__(self, name):
        super().__init__(name)


class SingleClassificationTask(ClassificationTask):
    ''' Generic sentence pair classification '''

    def __init__(self, name, n_classes):
        super().__init__(name)
        self.n_classes = n_classes
        self.scorer1 = CategoricalAccuracy()
        self.scorer2 = None
        self.val_metric = "%s_accuracy" % self.name
        self.val_metric_decreases = False

    def truncate(self, max_seq_len, sos_tok="<SOS>", eos_tok="<EOS>"):
        self.train_data_text = [truncate(self.train_data_text[0], max_seq_len,
                                         sos_tok, eos_tok), self.train_data_text[1]]
        self.val_data_text = [truncate(self.val_data_text[0], max_seq_len,
                                       sos_tok, eos_tok), self.val_data_text[1]]
        self.test_data_text = [truncate(self.test_data_text[0], max_seq_len,
                                        sos_tok, eos_tok), self.test_data_text[1]]

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        return {'accuracy': acc}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        return process_single_pair_task_split(split, indexers, is_pair=False)


class PairClassificationTask(ClassificationTask):
    ''' Generic sentence pair classification '''

    def __init__(self, name, n_classes):
        super().__init__(name)
        self.n_classes = n_classes
        self.scorer1 = CategoricalAccuracy()
        self.scorer2 = None
        self.val_metric = "%s_accuracy" % self.name
        self.val_metric_decreases = False

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        return {'accuracy': acc}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        return process_single_pair_task_split(split, indexers, is_pair=True)


# SRL CoNLL 2005, formulated as an edge-labeling task.
@register_task('edges-srl-conll2005', rel_path='edges/srl_conll2005',
               label_file="labels.txt", files_by_split={
                   'train': "train.edges.json",
                   'val': "dev.edges.json",
                   'test': "test.wsj.edges.json",
               }, is_symmetric=False)
# SRL CoNLL 2012 (OntoNotes), formulated as an edge-labeling task.
@register_task('edges-srl-conll2012', rel_path='edges/srl_conll2012',
               label_file="labels.txt", files_by_split={
                   'train': "train.edges.json",
                   'val': "dev.edges.json",
                   'test': "test.edges.json",
               }, is_symmetric=False)
# SPR1, as an edge-labeling task (multilabel).
@register_task('edges-spr1', rel_path='edges/spr1',
               label_file="labels.txt", files_by_split={
                   'train': "spr1.train.json",
                   'val': "spr1.dev.json",
                   'test': "spr1.test.json",
               }, is_symmetric=False)
# SPR2, as an edge-labeling task (multilabel).
@register_task('edges-spr2', rel_path='edges/spr2',
               label_file="labels.txt", files_by_split={
                   'train': "train.edges.json",
                   'val': "dev.edges.json",
                   'test': "test.edges.json",
               }, is_symmetric=False)
# Definite pronoun resolution. Two labels.
@register_task('edges-dpr', rel_path='edges/dpr',
               label_file="labels.txt", files_by_split={
                   'train': "train.edges.json",
                   'val': "dev.edges.json",
                   'test': "test.edges.json",
               }, is_symmetric=False)
# Coreference on OntoNotes corpus. Two labels.
@register_task('edges-coref-ontonotes', rel_path='edges/ontonotes-coref',
               label_file="labels.txt", files_by_split={
                   'train': "train.edges.json",
                   'val': "dev.edges.json",
                   'test': "test.edges.json",
               }, is_symmetric=False)
# Re-processed version of the above, via AllenNLP data loaders.
@register_task('edges-coref-ontonotes-conll',
               rel_path='edges/ontonotes-coref-conll',
               label_file="labels.txt", files_by_split={
                   'train': "coref_conll_ontonotes_en_train.json",
                   'val': "coref_conll_ontonotes_en_dev.json",
                   'test': "coref_conll_ontonotes_en_test.json",
               }, is_symmetric=False)
# Entity type labeling on CoNLL 2003.
@register_task('edges-ner-conll2003', rel_path='edges/ner_conll2003',
               label_file="labels.txt", files_by_split={
                   'train': "CoNLL-2003_train.json",
                   'val': "CoNLL-2003_dev.json",
                   'test': "CoNLL-2003_test.json",
               }, single_sided=True)
# Entity type labeling on OntoNotes.
@register_task('edges-ner-ontonotes',
               rel_path='edges/ontonotes-ner',
               label_file="labels.txt", files_by_split={
                   'train': "ner_ontonotes_en_train.json",
                   'val': "ner_ontonotes_en_dev.json",
                   'test': "ner_ontonotes_en_test.json",
               }, single_sided=True)
# Dependency edge labeling on UD treebank (GUM). Use 'ewt' version instead.
@register_task('edges-dep-labeling', rel_path='edges/dep',
               label_file="labels.txt", files_by_split={
                   'train': "train.json",
                   'val': "dev.json",
                   'test': "test.json",
               }, is_symmetric=False)
# Dependency edge labeling on English Web Treebank (UD).
@register_task('edges-dep-labeling-ewt', rel_path='edges/dep_ewt',
               label_file="labels.txt", files_by_split={
                   'train': "train.edges.json",
                   'val': "dev.edges.json",
                   'test': "test.edges.json",
               }, is_symmetric=False)
# PTB constituency membership / labeling.
@register_task('edges-constituent-ptb', rel_path='edges/ptb-membership',
               label_file="labels.txt", files_by_split={
                   'train': "ptb_train.json",
                   'val': "ptb_dev.json",
                   'test': "ptb_test.json",
               }, single_sided=True)
# Constituency membership / labeling on OntoNotes.
@register_task('edges-constituent-ontonotes',
               rel_path='edges/ontonotes-constituents',
               label_file="labels.txt", files_by_split={
                   'train': "consts_ontonotes_en_train.json",
                   'val': "consts_ontonotes_en_dev.json",
                   'test': "consts_ontonotes_en_test.json",
               }, single_sided=True)
# CCG tagging (tokens only).
@register_task('edges-ccg-tag', rel_path='edges/ccg_tag',
               label_file="labels.txt", files_by_split={
                   'train': "ccg.tag.train.json",
                   'val': "ccg.tag.dev.json",
                   'test': "ccg.tag.test.json",
               }, single_sided=True)
# CCG parsing (constituent labeling).
@register_task('edges-ccg-parse', rel_path='edges/ccg_parse',
               label_file="labels.txt", files_by_split={
                   'train': "ccg.parse.train.json",
                   'val': "ccg.parse.dev.json",
                   'test': "ccg.parse.test.json",
               }, single_sided=True)
class EdgeProbingTask(Task):
    ''' Generic class for fine-grained edge probing.

    Acts as a classifier, but with multiple targets for each input text.

    Targets are of the form (span1, span2, label), where span1 and span2 are
    half-open token intervals [i, j).

    Subclass this for each dataset, or use register_task with appropriate kw
    args.
    '''
    @property
    def _tokenizer_suffix(self):
        ''' Suffix to make sure we use the correct source files. '''
        return ".retokenized." + self.tokenizer_name

    def __init__(self, path: str, max_seq_len: int,
                 name: str,
                 label_file: str=None,
                 files_by_split: Dict[str, str]=None,
                 is_symmetric: bool=False,
                 single_sided: bool=False):
        """Construct an edge probing task.

        path, max_seq_len, and name are passed by the code in preprocess.py;
        remaining arguments should be provided by a subclass constructor or via
        @register_task.

        Args:
            path: data directory
            max_seq_len: maximum sequence length (currently ignored)
            name: task name
            label_file: relative path to labels file
            files_by_split: split name ('train', 'val', 'test') mapped to
                relative filenames (e.g. 'train': 'train.json')
            is_symmetric: if true, span1 and span2 are assumed to be the same
                type and share parameters. Otherwise, we learn a separate
                projection layer and attention weight for each.
            single_sided: if true, only use span1.
        """
        super().__init__(name)

        assert label_file is not None
        assert files_by_split is not None
        self._files_by_split = {
            split: os.path.join(path, fname) + self._tokenizer_suffix
            for split, fname in files_by_split.items()
        }
        self._iters_by_split = self.load_data()
        self.max_seq_len = max_seq_len
        self.is_symmetric = is_symmetric
        self.single_sided = single_sided

        label_file = os.path.join(path, label_file)
        self.all_labels = list(utils.load_lines(label_file))
        self.n_classes = len(self.all_labels)
        # see add_task_label_namespace in preprocess.py
        self._label_namespace = self.name + "_labels"

        # Scorers
        #  self.acc_scorer = CategoricalAccuracy()  # multiclass accuracy
        self.mcc_scorer = FastMatthews()
        self.acc_scorer = BooleanAccuracy()  # binary accuracy
        self.f1_scorer = F1Measure(positive_label=1)  # binary F1 overall
        self.val_metric = "%s_f1" % self.name  # TODO: switch to MCC?
        self.val_metric_decreases = False

    def _stream_records(self, filename):
        skip_ctr = 0
        total_ctr = 0
        for record in utils.load_json_data(filename):
            total_ctr += 1
            # Skip records with empty targets.
            # TODO(ian): don't do this if generating negatives!
            if not record.get('targets', None):
                skip_ctr += 1
                continue
            yield record
        log.info("Read=%d, Skip=%d, Total=%d from %s",
                 total_ctr - skip_ctr, skip_ctr, total_ctr,
                 filename)

    @staticmethod
    def merge_preds(record: Dict, preds: Dict) -> Dict:
        """ Merge predictions into record, in-place.

        List-valued predictions should align to targets,
        and are attached to the corresponding target entry.

        Non-list predictions are attached to the top-level record.
        """
        record['preds'] = {}
        for target in record['targets']:
            target['preds'] = {}
        for key, val in preds.items():
            if isinstance(val, list):
                assert len(val) == len(record['targets'])
                for i, target in enumerate(record['targets']):
                    target['preds'][key] = val[i]
            else:
                # non-list predictions, attach to top-level preds
                record['preds'][key] = val
        return record

    def load_data(self):
        iters_by_split = collections.OrderedDict()
        for split, filename in self._files_by_split.items():
            #  # Lazy-load using RepeatableIterator.
            #  loader = functools.partial(utils.load_json_data,
            #                             filename=filename)
            #  iter = serialize.RepeatableIterator(loader)
            iter = list(self._stream_records(filename))
            iters_by_split[split] = iter
        return iters_by_split

    def get_split_text(self, split: str):
        ''' Get split text as iterable of records.

        Split should be one of 'train', 'val', or 'test'.
        '''
        return self._iters_by_split[split]

    def get_num_examples(self, split_text):
        ''' Return number of examples in the result of get_split_text.

        Subclass can override this if data is not stored in column format.
        '''
        return len(split_text)

    def _make_span_field(self, s, text_field, offset=1):
        return SpanField(s[0] + offset, s[1] - 1 + offset, text_field)

    def make_instance(self, record, idx, indexers) -> Type[Instance]:
        """Convert a single record to an AllenNLP Instance."""
        tokens = record['text'].split()  # already space-tokenized by Moses
        tokens = [utils.SOS_TOK] + tokens + [utils.EOS_TOK]
        text_field = _sentence_to_text_field(tokens, indexers)

        d = {}
        d["idx"] = MetadataField(idx)

        d['input1'] = text_field

        d['span1s'] = ListField([self._make_span_field(t['span1'], text_field, 1)
                                 for t in record['targets']])
        if not self.single_sided:
            d['span2s'] = ListField([self._make_span_field(t['span2'], text_field, 1)
                                     for t in record['targets']])

        # Always use multilabel targets, so be sure each label is a list.
        labels = [utils.wrap_singleton_string(t['label'])
                  for t in record['targets']]
        d['labels'] = ListField([MultiLabelField(label_set,
                                                 label_namespace=self._label_namespace,
                                                 skip_indexing=False)
                                 for label_set in labels])
        return Instance(d)

    def process_split(self, records, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        def _map_fn(r, idx): return self.make_instance(r, idx, indexers)
        return map(_map_fn, records, itertools.count())

    def get_all_labels(self) -> List[str]:
        return self.all_labels

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        for split, iter in self._iters_by_split.items():
            # Don't use test set for vocab building.
            if split.startswith("test"):
                continue
            for record in iter:
                yield record["text"].split()

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        metrics = {}
        metrics['mcc'] = self.mcc_scorer.get_metric(reset)
        metrics['acc'] = self.acc_scorer.get_metric(reset)
        precision, recall, f1 = self.f1_scorer.get_metric(reset)
        metrics['precision'] = precision
        metrics['recall'] = recall
        metrics['f1'] = f1
        return metrics


class PairRegressionTask(RegressionTask):
    ''' Generic sentence pair classification '''

    def __init__(self, name):
        super().__init__(name)
        self.n_classes = 1
        self.scorer1 = Average()  # for average MSE
        self.scorer2 = None
        self.val_metric = "%s_mse" % self.name
        self.val_metric_decreases = True

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        mse = self.scorer1.get_metric(reset)
        return {'mse': mse}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        return process_single_pair_task_split(split, indexers, is_pair=True,
                                              classification=False)


class PairOrdinalRegressionTask(RegressionTask):
    ''' Generic sentence pair ordinal regression.
        Currently just doing regression but added new class
        in case we find a good way to implement ordinal regression with NN'''

    def __init__(self, name):
        super().__init__(name)
        self.n_classes = 1
        self.scorer1 = Average()  # for average MSE
        self.scorer2 = Correlation('spearman')
        self.val_metric = "%s_1-mse" % self.name
        self.val_metric_decreases = False

    def get_metrics(self, reset=False):
        mse = self.scorer1.get_metric(reset)
        spearmanr = self.scorer2.get_metric(reset)
        return {'1-mse': 1 - mse,
                'mse': mse,
                'spearmanr': spearmanr}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        return process_single_pair_task_split(split, indexers, is_pair=True,
                                              classification=False)


class SequenceGenerationTask(Task):
    ''' Generic sentence generation task '''

    def __init__(self, name):
        super().__init__(name)
        self.scorer1 = Average()  # for average BLEU or something
        self.scorer2 = None
        self.val_metric = "%s_bleu" % self.name
        self.val_metric_decreases = False
        log.warning("BLEU scoring is turned off (current code in progress)."
                    "Please use outputed prediction files to score offline")

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        bleu = self.scorer1.get_metric(reset)
        return {'bleu': bleu}


class RankingTask(Task):
    ''' Generic sentence ranking task, given some input '''

    def __init__(self, name):
        super().__init__(name)


class LanguageModelingTask(SequenceGenerationTask):
    """Generic language modeling task
    See base class: SequenceGenerationTask
    Attributes:
        max_seq_len: (int) maximum sequence length
        min_seq_len: (int) minimum sequence length
        target_indexer: (Indexer Obejct) Indexer used for target
        files_by_split: (dict) files for three data split (train, val, test)
    """

    def __init__(self, path, max_seq_len, name):
        """Init class
        Args:
            path: (str) path that the data files are stored
            max_seq_len: (int) maximum length of one sequence
            name: (str) task name
        """
        super().__init__(name)
        self.scorer1 = Average()
        self.scorer2 = None
        self.val_metric = "%s_perplexity" % self.name
        self.val_metric_decreases = True
        self.max_seq_len = max_seq_len
        self.min_seq_len = 0
        self.target_indexer = {"words": SingleIdTokenIndexer(namespace="tokens")}
        self.files_by_split = {'train': os.path.join(path, "train.txt"),
                               'val': os.path.join(path, "valid.txt"),
                               'test': os.path.join(path, "test.txt")}

    def count_examples(self):
        """Computes number of samples
        Assuming every line is one example.
        """
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            example_counts[split] = sum(1 for line in open(split_path))
        self.example_counts = example_counts

    def get_metrics(self, reset=False):
        """Get metrics specific to the task
        Args:
            reset: (boolean) reset any accumulators or internal state
        """
        nll = self.scorer1.get_metric(reset)
        return {'perplexity': math.exp(nll)}

    def load_data(self, path):
        """Loading data file and tokenizing the text
        Args:
            path: (str) data file path
        """
        with open(path) as txt_fh:
            for row in txt_fh:
                toks = row.strip()
                if not toks:
                    continue
                yield process_sentence(toks, self.max_seq_len)

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        """Process a language modeling split by indexing and creating fields.
        Args:
            split: (list) a single list of sentences
            indexers: (Indexer object) indexer to index input words
        """
        def _make_instance(sent):
            ''' Forward targs adds <s> as a target for input </s>
            and bwd targs adds </s> as a target for input <s>
            to avoid issues with needing to strip extra tokens
            in the input for each direction '''
            d = {}
            d["input"] = _sentence_to_text_field(sent, indexers)
            d["targs"] = _sentence_to_text_field(sent[1:] + [sent[0]], self.target_indexer)
            d["targs_b"] = _sentence_to_text_field([sent[-1]] + sent[:-1], self.target_indexer)
            return Instance(d)
        for sent in split:
            yield _make_instance(sent)

    def get_split_text(self, split: str):
        """Get split text as iterable of records.
        Args:
            split: (str) should be one of 'train', 'val', or 'test'.
        """
        return self.load_data(self.files_by_split[split])

    def get_sentences(self) -> Iterable[Sequence[str]]:
        """Yield sentences, used to compute vocabulary.
        """
        for split in self.files_by_split:
            # Don't use test set for vocab building.
            if split.startswith("test"):
                continue
            path = self.files_by_split[split]
            for sent in self.load_data(path):
                yield sent


class WikiTextLMTask(LanguageModelingTask):
    """ Language modeling on a Wikitext dataset
    See base class: LanguageModelingTask
    """

    def __init__(self, path, max_seq_len, name="wiki"):
        super().__init__(path, max_seq_len, name)

    def load_data(self, path):
        ''' Rather than return a whole list of examples, stream them '''
        nonatomics_toks = [UNK_TOK_ALLENNLP, '<unk>']
        with open(path) as txt_fh:
            for row in txt_fh:
                toks = row.strip()
                if not toks:
                    continue
                # WikiText103 preprocesses unknowns as '<unk>'
                # which gets tokenized as '@', '@', 'UNKNOWN', ...
                # We replace to avoid that
                sent = _atomic_tokenize(toks, UNK_TOK_ATOMIC, nonatomics_toks, self.max_seq_len)
                # we also filtering out headers (artifact of the data)
                # which are processed to have multiple = signs
                if sent.count("=") >= 2 or len(toks) < self.min_seq_len + 2:
                    continue
                yield sent


@register_task('wiki103', rel_path='WikiText103/')
class WikiText103LMTask(WikiTextLMTask):
    """Language modeling task on Wikitext 103
    See base class: WikiTextLMTask
    """

    def __init__(self, path, max_seq_len, name="wiki103"):
        super().__init__(path, max_seq_len, name)
        self.files_by_split = {'train': os.path.join(path, "train.sentences.txt"),
                               'val': os.path.join(path, "valid.sentences.txt"),
                               'test': os.path.join(path, "test.sentences.txt")}


@register_task('bwb', rel_path='BWB/')
class BWBLMTask(LanguageModelingTask):
    """Language modeling task on Billion Word Benchmark
    See base class: LanguageModelingTask
    """

    def __init__(self, path, max_seq_len, name="bwb"):
        super().__init__(path, max_seq_len, name)
        self.max_seq_len = max_seq_len


@register_task('sst', rel_path='SST-2/')
class SSTTask(SingleClassificationTask):
    ''' Task class for Stanford Sentiment Treebank.  '''

    def __init__(self, path, max_seq_len, name="sst"):
        ''' '''
        super(SSTTask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.val_data_text[0]

    def load_data(self, path, max_seq_len):
        ''' Load data '''
        tr_data = load_tsv(os.path.join(path, 'train.tsv'), max_seq_len,
                           s1_idx=0, s2_idx=None, targ_idx=1, skip_rows=1)
        val_data = load_tsv(os.path.join(path, 'dev.tsv'), max_seq_len,
                            s1_idx=0, s2_idx=None, targ_idx=1, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=None, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading SST data.")


@register_task('reddit', rel_path='Reddit_2008/')
@register_task('reddit_dummy', rel_path='Reddit_2008_TestSample/')
@register_task('reddit_3.4G', rel_path='Reddit_3.4G/')
@register_task('reddit_13G', rel_path='Reddit_13G/')
@register_task('reddit_softmax', rel_path='Reddit_2008/')
class RedditTask(RankingTask):
    ''' Task class for Reddit data.  '''

    def __init__(self, path, max_seq_len, name="reddit"):
        ''' '''
        super().__init__(name)
        self.scorer1 = Average()  # CategoricalAccuracy()
        self.scorer2 = None
        self.val_metric = "%s_accuracy" % self.name
        self.val_metric_decreases = False
        self.files_by_split = {split: os.path.join(path, "%s.csv" % split) for
                               split in ["train", "val", "test"]}
        self.max_seq_len = max_seq_len

    def get_split_text(self, split: str):
        ''' Get split text as iterable of records.

        Split should be one of 'train', 'val', or 'test'.
        '''
        return self.load_data(self.files_by_split[split])

    def load_data(self, path):
        ''' Load data '''
        with open(path, 'r') as txt_fh:
            for row in txt_fh:
                row = row.strip().split('\t')
                if len(row) < 4 or not row[2] or not row[3]:
                    continue
                sent1 = process_sentence(row[2], self.max_seq_len)
                sent2 = process_sentence(row[3], self.max_seq_len)
                targ = 1
                yield (sent1, sent2, targ)

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        for split in self.files_by_split:
            # Don't use test set for vocab building.
            if split.startswith("test"):
                continue
            path = self.files_by_split[split]
            for sent1, sent2, _ in self.load_data(path):
                yield sent1
                yield sent2

    def count_examples(self):
        ''' Compute here b/c we're streaming the sentences. '''
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            example_counts[split] = sum(1 for line in open(split_path))
        self.example_counts = example_counts

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        def _make_instance(input1, input2, labels):
            d = {}
            d["input1"] = _sentence_to_text_field(input1, indexers)
            #d['sent1_str'] = MetadataField(" ".join(input1[1:-1]))
            d["input2"] = _sentence_to_text_field(input2, indexers)
            #d['sent2_str'] = MetadataField(" ".join(input2[1:-1]))
            d["labels"] = LabelField(labels, label_namespace="labels",
                                     skip_indexing=True)
            return Instance(d)

        for sent1, sent2, trg in split:
            yield _make_instance(sent1, sent2, trg)

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        return {'accuracy': acc}


@register_task('reddit_pair_classif', rel_path='Reddit_2008/')
@register_task('reddit_pair_classif_dummy', rel_path='Reddit_2008_TestSample/')
@register_task('reddit_pair_classif_3.4G', rel_path='Reddit_3.4G/')
class RedditPairClassificationTask(PairClassificationTask):
    ''' Task class for Reddit data.  '''

    def __init__(self, path, max_seq_len, name="reddit_PairClassi"):
        ''' '''
        super().__init__(name, 2)
        self.scorer2 = None
        self.val_metric = "%s_accuracy" % self.name
        self.val_metric_decreases = False
        self.files_by_split = {split: os.path.join(path, "%s.csv" % split) for
                               split in ["train", "val", "test"]}
        self.max_seq_len = max_seq_len

    def get_split_text(self, split: str):
        ''' Get split text as iterable of records.

        Split should be one of 'train', 'val', or 'test'.
        '''
        return self.load_data(self.files_by_split[split])

    def load_data(self, path):
        ''' Load data '''
        with open(path, 'r') as txt_fh:
            for row in txt_fh:
                row = row.strip().split('\t')
                if len(row) < 4 or not row[2] or not row[3]:
                    continue
                sent1 = process_sentence(row[2], self.max_seq_len)
                sent2 = process_sentence(row[3], self.max_seq_len)
                targ = 1
                yield (sent1, sent2, targ)

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        for split in self.files_by_split:
            # Don't use test set for vocab building.
            if split.startswith("test"):
                continue
            path = self.files_by_split[split]
            for sent1, sent2, _ in self.load_data(path):
                yield sent1
                yield sent2

    def count_examples(self):
        ''' Compute here b/c we're streaming the sentences. '''
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            example_counts[split] = sum(1 for line in open(split_path))
        self.example_counts = example_counts

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        def _make_instance(input1, input2, labels):
            d = {}
            d["input1"] = _sentence_to_text_field(input1, indexers)
            #d['sent1_str'] = MetadataField(" ".join(input1[1:-1]))
            d["input2"] = _sentence_to_text_field(input2, indexers)
            #d['sent2_str'] = MetadataField(" ".join(input2[1:-1]))
            d["labels"] = LabelField(labels, label_namespace="labels",
                                     skip_indexing=True)
            return Instance(d)

        for sent1, sent2, trg in split:
            yield _make_instance(sent1, sent2, trg)

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        return {'accuracy': acc}


@register_task('mt_pair_classif', rel_path='wmt14_en_de_local/')
@register_task('mt_pair_classif_dummy', rel_path='wmt14_en_de_mini/')
class MTDataPairClassificationTask(RedditPairClassificationTask):
    ''' Task class for MT data pair classification using standard setup.
        RedditPairClassificationTask and MTDataPairClassificationTask are same tasks with different data
    '''

    def __init__(self, path, max_seq_len, name="mt_data_PairClassi"):
        ''' '''
        super().__init__(path, max_seq_len, name)
        self.files_by_split = {split: os.path.join(path, "%s.txt" % split) for
                               split in ["train", "val", "test"]}

    def load_data(self, path):
        ''' Load data '''
        with codecs.open(path, 'r', 'utf-8', errors='ignore') as txt_fh:
            for row in txt_fh:
                row = row.strip().split('\t')
                if len(row) < 2 or not row[0] or not row[1]:
                    continue
                sent1 = process_sentence(row[0], self.max_seq_len)
                sent2 = process_sentence(row[1], self.max_seq_len)
                targ = 1
                yield (sent1, sent2, targ)

    def count_examples(self):
        ''' Compute here b/c we're streaming the sentences. '''
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            example_counts[split] = sum(
                1 for line in codecs.open(
                    split_path, 'r', 'utf-8', errors='ignore'))
        self.example_counts = example_counts


@register_task('cola', rel_path='CoLA/')
class CoLATask(SingleClassificationTask):
    '''Class for Warstdadt acceptability task'''

    def __init__(self, path, max_seq_len, name="acceptability"):
        ''' '''
        super(CoLATask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.val_data_text[0]
        self.val_metric = "%s_mcc" % self.name
        self.val_metric_decreases = False
        #self.scorer1 = Average()
        self.scorer1 = Correlation("matthews")
        self.scorer2 = CategoricalAccuracy()

    def load_data(self, path, max_seq_len):
        '''Load the data'''
        tr_data = load_tsv(os.path.join(path, "train.tsv"), max_seq_len,
                           s1_idx=3, s2_idx=None, targ_idx=1)
        val_data = load_tsv(os.path.join(path, "dev.tsv"), max_seq_len,
                            s1_idx=3, s2_idx=None, targ_idx=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=None, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading CoLA.")

    def get_metrics(self, reset=False):
        return {'mcc': self.scorer1.get_metric(reset),
                'accuracy': self.scorer2.get_metric(reset)}


@register_task('qqp', rel_path='QQP/')
class QQPTask(PairClassificationTask):
    ''' Task class for Quora Question Pairs. '''

    def __init__(self, path, max_seq_len, name="qqp"):
        super().__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]
        self.scorer2 = F1Measure(1)
        self.val_metric = "%s_acc_f1" % name
        self.val_metric_decreases = False

    def load_data(self, path, max_seq_len):
        '''Process the dataset located at data_file.'''
        tr_data = load_tsv(os.path.join(path, "train.tsv"), max_seq_len,
                           s1_idx=3, s2_idx=4, targ_idx=5, skip_rows=1)
        val_data = load_tsv(os.path.join(path, "dev.tsv"), max_seq_len,
                            s1_idx=3, s2_idx=4, targ_idx=5, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading QQP data.")

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        pcs, rcl, f1 = self.scorer2.get_metric(reset)
        return {'acc_f1': (acc + f1) / 2, 'accuracy': acc, 'f1': f1,
                'precision': pcs, 'recall': rcl}


@register_task('qqp-alt', rel_path='QQP/')
class QQPAltTask(QQPTask):
    ''' Task class for Quora Question Pairs.

    Identical to QQPTask class, but it can be handy to have two when controlling model settings.
    '''

    def __init__(self, path, max_seq_len, name="qqp-alt"):
        '''QQP'''
        super(QQPAltTask, self).__init__(path, max_seq_len, name)


class MultiNLISingleGenreTask(PairClassificationTask):
    ''' Task class for Multi-Genre Natural Language Inference, Fiction genre.'''

    def __init__(self, path, max_seq_len, genre, name):
        '''MNLI'''
        super(MultiNLISingleGenreTask, self).__init__(name, 3)
        self.load_data(path, max_seq_len, genre)
        self.scorer2 = None
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len, genre):
        '''Process the dataset located at path. We only use the in-genre matche data.'''
        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}

        tr_data = load_tsv(
            os.path.join(
                path,
                'train.tsv'),
            max_seq_len,
            s1_idx=8,
            s2_idx=9,
            targ_idx=11,
            targ_map=targ_map,
            idx_idx=0,
            skip_rows=1,
            filter_idx=3,
            filter_value=genre)

        val_matched_data = load_tsv(
            os.path.join(
                path,
                'dev_matched.tsv'),
            max_seq_len,
            s1_idx=8,
            s2_idx=9,
            targ_idx=11,
            targ_map=targ_map,
            idx_idx=0,
            skip_rows=1,
            filter_idx=3,
            filter_value=genre)

        te_matched_data = load_tsv(
            os.path.join(
                path,
                'test_matched.tsv'),
            max_seq_len,
            s1_idx=8,
            s2_idx=9,
            targ_idx=None,
            idx_idx=0,
            skip_rows=1,
            filter_idx=3,
            filter_value=genre)

        self.train_data_text = tr_data
        self.val_data_text = val_matched_data
        self.test_data_text = te_matched_data
        log.info("\tFinished loading MNLI " + genre + " data.")

    def get_metrics(self, reset=False):
        ''' No F1 '''
        return {'accuracy': self.scorer1.get_metric(reset)}


@register_task('mnli-fiction', rel_path='MNLI/')
class MultiNLIFictionTask(MultiNLISingleGenreTask):
    ''' Task class for Multi-Genre Natural Language Inference, Fiction genre.'''

    def __init__(self, path, max_seq_len, name="mnli-fiction"):
        '''MNLI'''
        super(
            MultiNLIFictionTask,
            self).__init__(
            path,
            max_seq_len,
            genre="fiction",
            name=name)


@register_task('mnli-slate', rel_path='MNLI/')
class MultiNLISlateTask(MultiNLISingleGenreTask):
    ''' Task class for Multi-Genre Natural Language Inference, Fiction genre.'''

    def __init__(self, path, max_seq_len, name="mnli-slate"):
        '''MNLI'''
        super(MultiNLISlateTask, self).__init__(path, max_seq_len, genre="slate", name=name)


@register_task('mnli-government', rel_path='MNLI/')
class MultiNLIGovernmentTask(MultiNLISingleGenreTask):
    ''' Task class for Multi-Genre Natural Language Inference, Fiction genre.'''

    def __init__(self, path, max_seq_len, name="mnli-government"):
        '''MNLI'''
        super(
            MultiNLIGovernmentTask,
            self).__init__(
            path,
            max_seq_len,
            genre="government",
            name=name)


@register_task('mnli-telephone', rel_path='MNLI/')
class MultiNLITelephoneTask(MultiNLISingleGenreTask):
    ''' Task class for Multi-Genre Natural Language Inference, Fiction genre.'''

    def __init__(self, path, max_seq_len, name="mnli-telephone"):
        '''MNLI'''
        super(
            MultiNLITelephoneTask,
            self).__init__(
            path,
            max_seq_len,
            genre="telephone",
            name=name)


@register_task('mnli-travel', rel_path='MNLI/')
class MultiNLITravelTask(MultiNLISingleGenreTask):
    ''' Task class for Multi-Genre Natural Language Inference, Fiction genre.'''

    def __init__(self, path, max_seq_len, name="mnli-travel"):
        '''MNLI'''
        super(
            MultiNLITravelTask,
            self).__init__(
            path,
            max_seq_len,
            genre="travel",
            name=name)


@register_task('mrpc', rel_path='MRPC/')
class MRPCTask(PairClassificationTask):
    ''' Task class for Microsoft Research Paraphase Task.  '''

    def __init__(self, path, max_seq_len, name="mrpc"):
        ''' '''
        super(MRPCTask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]
        self.scorer2 = F1Measure(1)
        self.val_metric = "%s_acc_f1" % name
        self.val_metric_decreases = False

    def load_data(self, path, max_seq_len):
        ''' Process the dataset located at path.  '''
        tr_data = load_tsv(os.path.join(path, "train.tsv"), max_seq_len,
                           s1_idx=3, s2_idx=4, targ_idx=0, skip_rows=1)
        val_data = load_tsv(os.path.join(path, "dev.tsv"), max_seq_len,
                            s1_idx=3, s2_idx=4, targ_idx=0, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=3, s2_idx=4, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading MRPC data.")

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        pcs, rcl, f1 = self.scorer2.get_metric(reset)
        return {'acc_f1': (acc + f1) / 2, 'accuracy': acc, 'f1': f1,
                'precision': pcs, 'recall': rcl}


@register_task('sts-b', rel_path='STS-B/')
class STSBTask(PairRegressionTask):
    ''' Task class for Sentence Textual Similarity Benchmark.  '''

    def __init__(self, path, max_seq_len, name="sts_benchmark"):
        ''' '''
        super(STSBTask, self).__init__(name)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]
        #self.scorer1 = Average()
        #self.scorer2 = Average()
        self.scorer1 = Correlation("pearson")
        self.scorer2 = Correlation("spearman")
        self.val_metric = "%s_corr" % self.name
        self.val_metric_decreases = False

    def load_data(self, path, max_seq_len):
        ''' Load data '''
        tr_data = load_tsv(os.path.join(path, 'train.tsv'), max_seq_len, skip_rows=1,
                           s1_idx=7, s2_idx=8, targ_idx=9, targ_fn=lambda x: float(x) / 5)
        val_data = load_tsv(os.path.join(path, 'dev.tsv'), max_seq_len, skip_rows=1,
                            s1_idx=7, s2_idx=8, targ_idx=9, targ_fn=lambda x: float(x) / 5)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=7, s2_idx=8, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading STS Benchmark data.")

    def get_metrics(self, reset=False):
        pearsonr = self.scorer1.get_metric(reset)
        spearmanr = self.scorer2.get_metric(reset)
        return {'corr': (pearsonr + spearmanr) / 2,
                'pearsonr': pearsonr, 'spearmanr': spearmanr}


@register_task('sts-b-alt', rel_path='STS-B/')
class STSBAltTask(STSBTask):
    ''' Task class for Sentence Textual Similarity Benchmark.

    Identical to STSBTask class, but it can be handy to have two when controlling model settings.
    '''

    def __init__(self, path, max_seq_len, name="sts_benchmark-alt"):
        '''STSB'''
        super(STSBAltTask, self).__init__(path, max_seq_len, name)


@register_task('snli', rel_path='SNLI/')
class SNLITask(PairClassificationTask):
    ''' Task class for Stanford Natural Language Inference '''

    def __init__(self, path, max_seq_len, name="snli"):
        ''' Do stuff '''
        super(SNLITask, self).__init__(name, 3)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        ''' Process the dataset located at path.  '''
        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}
        tr_data = load_tsv(os.path.join(path, "train.tsv"), max_seq_len, targ_map=targ_map,
                           s1_idx=7, s2_idx=8, targ_idx=-1, skip_rows=1)
        val_data = load_tsv(os.path.join(path, "dev.tsv"), max_seq_len, targ_map=targ_map,
                            s1_idx=7, s2_idx=8, targ_idx=-1, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=7, s2_idx=8, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading SNLI data.")


@register_task('mnli', rel_path='MNLI/')
class MultiNLITask(PairClassificationTask):
    ''' Task class for Multi-Genre Natural Language Inference '''

    def __init__(self, path, max_seq_len, name="mnli"):
        '''MNLI'''
        super(MultiNLITask, self).__init__(name, 3)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        '''Process the dataset located at path.'''
        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}
        tr_data = load_tsv(os.path.join(path, 'train.tsv'), max_seq_len,
                           s1_idx=8, s2_idx=9, targ_idx=11, targ_map=targ_map, skip_rows=1)

        # Warning to anyone who edits this: The reference label is column *15*, not 11 as above.
        val_matched_data = load_tsv(os.path.join(path, 'dev_matched.tsv'), max_seq_len,
                                    s1_idx=8, s2_idx=9, targ_idx=15, targ_map=targ_map, skip_rows=1)
        val_mismatched_data = load_tsv(os.path.join(path, 'dev_mismatched.tsv'), max_seq_len,
                                       s1_idx=8, s2_idx=9, targ_idx=15, targ_map=targ_map,
                                       skip_rows=1)
        val_data = [m + mm for m, mm in zip(val_matched_data, val_mismatched_data)]
        val_data = tuple(val_data)

        te_matched_data = load_tsv(os.path.join(path, 'test_matched.tsv'), max_seq_len,
                                   s1_idx=8, s2_idx=9, targ_idx=None, idx_idx=0, skip_rows=1)
        te_mismatched_data = load_tsv(os.path.join(path, 'test_mismatched.tsv'), max_seq_len,
                                      s1_idx=8, s2_idx=9, targ_idx=None, idx_idx=0, skip_rows=1)
        te_diagnostic_data = load_tsv(os.path.join(path, 'diagnostic.tsv'), max_seq_len,
                                      s1_idx=1, s2_idx=2, targ_idx=None, idx_idx=0, skip_rows=1)
        te_data = [m + mm + d for m, mm, d in
                   zip(te_matched_data, te_mismatched_data, te_diagnostic_data)]

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading MNLI data.")


@register_task('mnli-diagnostic', rel_path='MNLI/')
class MultiNLIDiagnosticTask(PairClassificationTask):
    ''' Task class for diagnostic on MNLI'''

    def __init__(self, path, max_seq_len, name="mnli-diagnostics"):
        super().__init__(name, 3)  # 3 is number of labels
        self.load_data_and_create_scorers(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data_and_create_scorers(self, path, max_seq_len):
        '''load MNLI diagnostics data. The tags for every column are loaded as indices.
        They will be converted to bools in preprocess_split function'''

        # Will create separate scorer for every tag. tag_group is the name of the
        # column it will have its own scorer
        def create_score_function(scorer, arg_to_scorer, tags_dict, tag_group):
            setattr(self, 'scorer__%s' % tag_group, scorer(arg_to_scorer))
            for index, tag in tags_dict.items():
                # 0 is missing value
                if index == 0:
                    continue
                setattr(self, "scorer__%s__%s" % (tag_group, tag), scorer(arg_to_scorer))

        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}
        diag_data_dic = load_diagnostic_tsv(
            os.path.join(
                path,
                'diagnostic-full.tsv'),
            max_seq_len,
            s1_idx=5,
            s2_idx=6,
            targ_idx=7,
            targ_map=targ_map,
            skip_rows=1)

        self.ix_to_lex_sem_dic = diag_data_dic['ix_to_lex_sem_dic']
        self.ix_to_pr_ar_str_dic = diag_data_dic['ix_to_pr_ar_str_dic']
        self.ix_to_logic_dic = diag_data_dic['ix_to_logic_dic']
        self.ix_to_knowledge_dic = diag_data_dic['ix_to_knowledge_dic']

        # Train, val, test splits are same. We only need one split but the code
        # probably expects all splits to be present.
        self.train_data_text = (
            diag_data_dic['sents1'],
            diag_data_dic['sents2'],
            diag_data_dic['targs'],
            diag_data_dic['idxs'],
            diag_data_dic['lex_sem'],
            diag_data_dic['pr_ar_str'],
            diag_data_dic['logic'],
            diag_data_dic['knowledge'])
        self.val_data_text = self.train_data_text
        self.test_data_text = self.train_data_text
        log.info("\tFinished loading MNLI Diagnostics data.")

        create_score_function(Correlation, "matthews", self.ix_to_lex_sem_dic, 'lex_sem')
        create_score_function(Correlation, "matthews", self.ix_to_pr_ar_str_dic, 'pr_ar_str')
        create_score_function(Correlation, "matthews", self.ix_to_logic_dic, 'logic')
        create_score_function(Correlation, "matthews", self.ix_to_knowledge_dic, 'knowledge')
        log.info("\tFinished creating Score functions for Diagnostics data.")

    def update_diagnostic_metrics(self, logits, labels, batch):
        # Updates scorer for every tag in a given column (tag_group) and also the
        # the scorer for the column itself.
        def update_scores_for_tag_group(ix_to_tags_dic, tag_group):
            for ix, tag in ix_to_tags_dic.items():
                # 0 is for missing tag so here we use it to update scorer for the column
                # itself (tag_group).
                if ix == 0:
                    # This will contain 1s on positions where at least one of the tags of this
                    # column is present.
                    mask = batch[tag_group]
                    scorer_str = "scorer__%s" % tag_group
                # This branch will update scorers of individual tags in the column
                else:
                    # batch contains_field for every tag. It's either 0 or 1.
                    mask = batch["%s__%s" % (tag_group, tag)]
                    scorer_str = "scorer__%s__%s" % (tag_group, tag)

                # This will take only values for which the tag is true.
                indices_to_pull = torch.nonzero(mask)
                # No example in the batch is labeled with the tag.
                if indices_to_pull.size()[0] == 0:
                    continue
                sub_labels = labels[indices_to_pull[:, 0]]
                sub_logits = logits[indices_to_pull[:, 0]]
                scorer = getattr(self, scorer_str)
                scorer(sub_logits, sub_labels)
            return

        # Updates scorers for each tag.
        update_scores_for_tag_group(self.ix_to_lex_sem_dic, 'lex_sem')
        update_scores_for_tag_group(self.ix_to_pr_ar_str_dic, 'pr_ar_str')
        update_scores_for_tag_group(self.ix_to_logic_dic, 'logic')
        update_scores_for_tag_group(self.ix_to_knowledge_dic, 'knowledge')

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''

        def create_labels_from_tags(fields_dict, ix_to_tag_dict, tag_arr, tag_group):
            # If there is something in this row then tag_group should be set to 1.
            is_tag_group = 1 if len(tag_arr) != 0 else 0
            fields_dict[tag_group] = LabelField(is_tag_group, label_namespace=tag_group,
                                                skip_indexing=True)
            # For every possible tag in the column set 1 if the tag is present for
            # this example, 0 otherwise.
            for ix, tag in ix_to_tag_dict.items():
                if ix == 0:
                    continue
                is_present = 1 if ix in tag_arr else 0
                fields_dict['%s__%s' % (tag_group, tag)] = LabelField(
                    is_present, label_namespace='%s__%s' % (tag_group, tag), skip_indexing=True)
            return

        def _make_instance(input1, input2, label, idx, lex_sem, pr_ar_str, logic, knowledge):
            ''' from multiple types in one column create multiple fields '''
            d = {}
            d["input1"] = _sentence_to_text_field(input1, indexers)
            d["input2"] = _sentence_to_text_field(input2, indexers)
            d["labels"] = LabelField(label, label_namespace="labels",
                                     skip_indexing=True)
            d["idx"] = LabelField(idx, label_namespace="idx",
                                  skip_indexing=True)
            d['sent1_str'] = MetadataField(" ".join(input1[1:-1]))
            d['sent2_str'] = MetadataField(" ".join(input2[1:-1]))

            # adds keys to dict "d" for every possible type in the column
            create_labels_from_tags(d, self.ix_to_lex_sem_dic, lex_sem, 'lex_sem')
            create_labels_from_tags(d, self.ix_to_pr_ar_str_dic, pr_ar_str, 'pr_ar_str')
            create_labels_from_tags(d, self.ix_to_logic_dic, logic, 'logic')
            create_labels_from_tags(d, self.ix_to_knowledge_dic, knowledge, 'knowledge')

            return Instance(d)

        instances = map(_make_instance, *split)
        #  return list(instances)
        return instances  # lazy iterator

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        collected_metrics = {}
        # We do not compute accuracy for this dataset but the eval function requires this key.
        collected_metrics["accuracy"] = 0

        def collect_metrics(ix_to_tag_dict, tag_group):
            for index, tag in ix_to_tag_dict.items():
                # Index 0 is used for missing data, here it will be used for score of the
                # whole category.
                if index == 0:
                    scorer_str = 'scorer__%s' % tag_group
                    scorer = getattr(self, scorer_str)
                    collected_metrics['%s' % (tag_group)] = scorer.get_metric(reset)
                else:
                    scorer_str = 'scorer__%s__%s' % (tag_group, tag)
                    scorer = getattr(self, scorer_str)
                    collected_metrics['%s__%s' % (tag_group, tag)] = scorer.get_metric(reset)

        collect_metrics(self.ix_to_lex_sem_dic, 'lex_sem')
        collect_metrics(self.ix_to_pr_ar_str_dic, 'pr_ar_str')
        collect_metrics(self.ix_to_logic_dic, 'logic')
        collect_metrics(self.ix_to_knowledge_dic, 'knowledge')
        return collected_metrics


@register_task('nli-prob', rel_path='NLI-Prob/')
class NLITypeProbingTask(PairClassificationTask):
    ''' Task class for Probing Task (NLI-type)'''

    def __init__(self, path, max_seq_len, name="nli-prob", probe_path="probe_dummy.tsv"):
        super(NLITypeProbingTask, self).__init__(name, 3)
        self.load_data(path, max_seq_len, probe_path)
        #  self.use_classifier = 'mnli'  # use .conf params instead
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len, probe_path):
        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}
        tr_data = load_tsv(os.path.join(path, 'train_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, targ_map=targ_map, skip_rows=0)
        val_data = load_tsv(os.path.join(path, probe_path), max_seq_len,
                            s1_idx=0, s2_idx=1, targ_idx=2, targ_map=targ_map, skip_rows=0)
        te_data = load_tsv(os.path.join(path, 'test_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, targ_map=targ_map, skip_rows=0)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading NLI-type probing data.")


@register_task('nli-prob-negation', rel_path='NLI-Prob/')
class NLITypeProbingTaskNeg(PairClassificationTask):

    def __init__(self, path, max_seq_len, name="nli-prob-negation", probe_path="probe_dummy.tsv"):
        super(NLITypeProbingTaskNeg, self).__init__(name, 3)
        self.load_data(path, max_seq_len, probe_path)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len, probe_path):
        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}
        tr_data = load_tsv(os.path.join(path, 'train_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, skip_rows=0)
        val_data = load_tsv(os.path.join(path, 'lexnegs.tsv'), max_seq_len,
                            s1_idx=8, s2_idx=9, targ_idx=10, targ_map=targ_map, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, skip_rows=0)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading negation data.")


@register_task('nli-prob-prepswap', rel_path='NLI-Prob/')
class NLITypeProbingTaskPrepswap(PairClassificationTask):

    def __init__(self, path, max_seq_len, name="nli-prob-prepswap", probe_path="probe_dummy.tsv"):
        super(NLITypeProbingTaskPrepswap, self).__init__(name, 3)
        self.load_data(path, max_seq_len, probe_path)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len, probe_path):
        tr_data = load_tsv(os.path.join(path, 'train_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, skip_rows=0)
        val_data = load_tsv(os.path.join(path, 'all.prepswap.turk.newlabels.tsv'), max_seq_len,
                            s1_idx=8, s2_idx=9, targ_idx=0, skip_rows=0)
        te_data = load_tsv(os.path.join(path, 'test_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, skip_rows=0)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading preposition swap data.")


@register_task('nps', rel_path='nps/')
class NPSTask(PairClassificationTask):

    def __init__(self, path, max_seq_len, name="nps", probe_path="probe_dummy.tsv"):
        super(NPSTask, self).__init__(name, 3)
        self.load_data(path, max_seq_len, probe_path)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len, probe_path):
        targ_map = {'neutral': 0, 'entailment': 1, 'contradiction': 2}
        tr_data = load_tsv(os.path.join(path, 'train_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, targ_map=targ_map, skip_rows=0)
        val_data = load_tsv(os.path.join(path, 'dev.tsv'), max_seq_len,
                            s1_idx=0, s2_idx=1, targ_idx=2, targ_map=targ_map, skip_rows=0)
        te_data = load_tsv(os.path.join(path, 'test_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, targ_map=targ_map, skip_rows=0)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading NP/S data.")


@register_task('nli-alt', rel_path='NLI-Prob/')
class NLITypeProbingAltTask(NLITypeProbingTask):
    ''' Task class for Alt Probing Task (NLI-type), NLITypeProbingTask with different indices'''

    def __init__(self, path, max_seq_len, name="nli-alt", probe_path="probe_dummy.tsv"):
        super(NLITypeProbingTask, self).__init__(name, 3)
        self.load_data(path, max_seq_len, probe_path)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len, probe_path):
        targ_map = {'0': 0, '1': 1, '2': 2}
        tr_data = load_tsv(os.path.join(path, 'train_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, targ_map=targ_map, skip_rows=0)
        val_data = load_tsv(
            os.path.join(
                path,
                probe_path),
            max_seq_len,
            idx_idx=0,
            s1_idx=9,
            s2_idx=10,
            targ_idx=1,
            targ_map=targ_map,
            skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test_dummy.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, targ_map=targ_map, skip_rows=0)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading NLI-alt probing data.")


@register_task('mnli-alt', rel_path='MNLI/')
class MultiNLIAltTask(MultiNLITask):
    ''' Task class for Multi-Genre Natural Language Inference.

    Identical to MultiNLI class, but it can be handy to have two when controlling model settings.
    '''

    def __init__(self, path, max_seq_len, name="mnli-alt"):
        '''MNLI'''
        super(MultiNLIAltTask, self).__init__(path, max_seq_len, name)


@register_task('rte', rel_path='RTE/')
class RTETask(PairClassificationTask):
    ''' Task class for Recognizing Textual Entailment 1, 2, 3, 5 '''

    def __init__(self, path, max_seq_len, name="rte"):
        ''' '''
        super(RTETask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        ''' Process the datasets located at path. '''
        targ_map = {"not_entailment": 0, "entailment": 1}
        tr_data = load_tsv(os.path.join(path, 'train.tsv'), max_seq_len, targ_map=targ_map,
                           s1_idx=1, s2_idx=2, targ_idx=3, skip_rows=1)
        val_data = load_tsv(os.path.join(path, 'dev.tsv'), max_seq_len, targ_map=targ_map,
                            s1_idx=1, s2_idx=2, targ_idx=3, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, idx_idx=0, skip_rows=1)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading RTE.")


@register_task('qnli', rel_path='QNLI/')
class QNLITask(PairClassificationTask):
    '''Task class for SQuAD NLI'''

    def __init__(self, path, max_seq_len, name="squad"):
        super(QNLITask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        '''Load the data'''
        targ_map = {'not_entailment': 0, 'entailment': 1}
        tr_data = load_tsv(os.path.join(path, "train.tsv"), max_seq_len, targ_map=targ_map,
                           s1_idx=1, s2_idx=2, targ_idx=3, skip_rows=1)
        val_data = load_tsv(os.path.join(path, "dev.tsv"), max_seq_len, targ_map=targ_map,
                            s1_idx=1, s2_idx=2, targ_idx=3, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading QNLI.")


@register_task('qnli-alt', rel_path='QNLI/')
class QNLIAltTask(QNLITask):
    ''' Task class for SQuAD NLI
    Identical to SQuAD NLI class, but it can be handy to have two when controlling model settings.
    '''

    def __init__(self, path, max_seq_len, name="squad-alt"):
        '''QNLI'''
        super(QNLIAltTask, self).__init__(path, max_seq_len, name)


@register_task('wnli', rel_path='WNLI/')
class WNLITask(PairClassificationTask):
    '''Class for Winograd NLI task'''

    def __init__(self, path, max_seq_len, name="winograd"):
        ''' '''
        super(WNLITask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        '''Load the data'''
        tr_data = load_tsv(os.path.join(path, "train.tsv"), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=3, skip_rows=1)
        val_data = load_tsv(os.path.join(path, "dev.tsv"), max_seq_len,
                            s1_idx=1, s2_idx=2, targ_idx=3, skip_rows=1)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, targ_idx=None, idx_idx=0, skip_rows=1)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading Winograd.")


@register_task('joci', rel_path='JOCI/')
class JOCITask(PairOrdinalRegressionTask):
    '''Class for JOCI ordinal regression task'''

    def __init__(self, path, max_seq_len, name="joci"):
        super(JOCITask, self).__init__(name)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        tr_data = load_tsv(os.path.join(path, 'train.tsv'), max_seq_len, skip_rows=1,
                           s1_idx=0, s2_idx=1, targ_idx=2)
        val_data = load_tsv(os.path.join(path, 'dev.tsv'), max_seq_len, skip_rows=1,
                            s1_idx=0, s2_idx=1, targ_idx=2)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len, skip_rows=1,
                           s1_idx=0, s2_idx=1, targ_idx=2)
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading JOCI data.")


class MTTask(SequenceGenerationTask):
    '''Machine Translation Task'''

    def __init__(self, path, max_seq_len, max_targ_v_size, name):
        ''' '''
        super().__init__(name)
        self.scorer1 = Average()
        self.scorer2 = Average()
        self.scorer3 = Average()
        self.val_metric = "%s_perplexity" % self.name
        self.val_metric_decreases = True
        self.max_seq_len = max_seq_len
        self._label_namespace = self.name + "_tokens"
        self.max_targ_v_size = max_targ_v_size
        self.target_indexer = {"words": SingleIdTokenIndexer(namespace=self._label_namespace)}
        self.files_by_split = {split: os.path.join(path, "%s.txt" % split) for
                               split in ["train", "val", "test"]}

    def get_split_text(self, split: str):
        ''' Get split text as iterable of records.

        Split should be one of 'train', 'val', or 'test'.
        '''
        return self.load_data(self.files_by_split[split])

    def get_all_labels(self) -> List[str]:
        ''' Build vocabulary and return it as a list '''
        word2freq = collections.Counter()
        for split in ["train", "val"]:
            for _, sent in self.load_data(self.files_by_split[split]):
                for word in sent:
                    word2freq[word] += 1
        return [w for w, _ in word2freq.most_common(self.max_targ_v_size)]

    def load_data(self, path):
        ''' Load data '''
        with codecs.open(path, 'r', 'utf-8', errors='ignore') as txt_fh:
            for row in txt_fh:
                row = row.strip().split('\t')
                if len(row) < 2 or not row[0] or not row[1]:
                    continue
                src_sent = process_sentence(row[0], self.max_seq_len)
                # target sentence sos_tok, eos_tok need to match Seq2SeqDecoder class
                tgt_sent = process_sentence(
                    row[1], self.max_seq_len,
                    sos_tok=allennlp_util.START_SYMBOL,
                    eos_tok=allennlp_util.END_SYMBOL,
                )
                yield (src_sent, tgt_sent)

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        for split in self.files_by_split:
            # Don't use test set for vocab building.
            if split.startswith("test"):
                continue
            path = self.files_by_split[split]
            yield from self.load_data(path)

    def count_examples(self):
        ''' Compute here b/c we're streaming the sentences. '''
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            example_counts[split] = sum(
                1 for line in codecs.open(
                    split_path, 'r', 'utf-8', errors='ignore'))
        self.example_counts = example_counts

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        def _make_instance(input, target):
            d = {}
            d["inputs"] = _sentence_to_text_field(input, indexers)
            d["targs"] = _sentence_to_text_field(target, self.target_indexer)  # this line changed
            return Instance(d)

        for sent1, sent2 in split:
            yield _make_instance(sent1, sent2)

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        avg_nll = self.scorer1.get_metric(reset)
        unk_ratio_macroavg = self.scorer3.get_metric(reset)
        return {
            'perplexity': math.exp(avg_nll),
            'bleu_score': 0,
            'unk_ratio_macroavg': unk_ratio_macroavg}


@register_task('wmt_debug', rel_path='wmt_debug/', max_targ_v_size=5000)
class MTDebug(MTTask):
    def __init__(self, path, max_seq_len, max_targ_v_size, name='wmt_debug'):
        ''' Demo task for MT with 10k training examples.'''
        super().__init__(path=path, max_seq_len=max_seq_len,
                         max_targ_v_size=max_targ_v_size, name=name)
        self.files_by_split = {"train": os.path.join(path, "train.txt"),
                               "val": os.path.join(path, "valid.txt"),
                               "test": os.path.join(path, "test.txt")}


@register_task('wmt17_en_ru', rel_path='wmt17_en_ru/', max_targ_v_size=20000)
class MTTaskEnRu(MTTask):
    def __init__(self, path, max_seq_len, max_targ_v_size, name='mt_en_ru'):
        ''' MT En-Ru'''
        super().__init__(path=path, max_seq_len=max_seq_len,
                         max_targ_v_size=max_targ_v_size, name=name)
        self.files_by_split = {"train": os.path.join(path, "train.txt"),
                               "val": os.path.join(path, "valid.txt"),
                               "test": os.path.join(path, "test.txt")}


@register_task('wmt14_en_de', rel_path='wmt14_en_de/', max_targ_v_size=20000)
class MTTaskEnDe(MTTask):
    def __init__(self, path, max_seq_len, max_targ_v_size, name='mt_en_de'):
        ''' MT En-De'''
        super().__init__(path=path, max_seq_len=max_seq_len,
                         max_targ_v_size=max_targ_v_size, name=name)

        self.files_by_split = {"train": os.path.join(path, "train.txt"),
                               "val": os.path.join(path, "valid.txt"),
                               "test": os.path.join(path, "test.txt")}


@register_task('reddit_s2s', rel_path='Reddit_2008/', max_targ_v_size=0)
@register_task('reddit_s2s_3.4G', rel_path='Reddit_3.4G/', max_targ_v_size=0)
@register_task('reddit_s2s_dummy', rel_path='Reddit_2008_TestSample/', max_targ_v_size=0)
class RedditSeq2SeqTask(MTTask):
    ''' Task for seq2seq using reddit data

    Note: max_targ_v_size doesn't do anything here b/c the
    target is in English'''

    def __init__(self, path, max_seq_len, max_targ_v_size, name='reddit_s2s'):
        super().__init__(path=path, max_seq_len=max_seq_len,
                         max_targ_v_size=max_targ_v_size, name=name)
        self._label_namespace = None
        self.target_indexer = {"words": SingleIdTokenIndexer("tokens")}
        self.files_by_split = {"train": os.path.join(path, "train.csv"),
                               "val": os.path.join(path, "val.csv"),
                               "test": os.path.join(path, "test.csv")}

    def load_data(self, path):
        ''' Load data '''
        with codecs.open(path, 'r', 'utf-8', errors='ignore') as txt_fh:
            for row in txt_fh:
                row = row.strip().split('\t')
                if len(row) < 4 or not row[2] or not row[3]:
                    continue
                src_sent = process_sentence(row[2], self.max_seq_len)
                tgt_sent = process_sentence(row[3], self.max_seq_len,
                                            sos_tok=allennlp_util.START_SYMBOL,
                                            eos_tok=allennlp_util.END_SYMBOL,
                                            )
                yield (src_sent, tgt_sent)


@register_task('wiki103_classif', rel_path='WikiText103/')
class Wiki103Classification(PairClassificationTask):
    '''Pair Classificaiton Task using Wiki103'''

    def __init__(self, path, max_seq_len, name="wiki103_classif"):
        super().__init__(name, 2)
        self.scorer2 = None
        self.val_metric = "%s_accuracy" % self.name
        self.val_metric_decreases = False
        self.files_by_split = {'train': os.path.join(path, "train.sentences.txt"),
                               'val': os.path.join(path, "valid.sentences.txt"),
                               'test': os.path.join(path, "test.sentences.txt")}
        self.max_seq_len = max_seq_len
        self.min_seq_len = 0

    def get_split_text(self, split: str):
        ''' Get split text as iterable of records.
        Split should be one of 'train', 'val', or 'test'.
        '''
        return self.load_data(self.files_by_split[split])

    def load_data(self, path):
        ''' Rather than return a whole list of examples, stream them
        See WikiTextLMTask for an explanation of the preproc'''
        nonatomics_toks = [UNK_TOK_ALLENNLP, '<unk>']
        with open(path) as txt_fh:
            for row in txt_fh:
                toks = row.strip()
                if not toks:
                    continue
                sent = _atomic_tokenize(toks, UNK_TOK_ATOMIC, nonatomics_toks, self.max_seq_len)
                if sent.count("=") >= 2 or len(toks) < self.min_seq_len + 2:
                    continue
                yield sent

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        for split in self.files_by_split:
            # Don't use test set for vocab building.
            if split.startswith("test"):
                continue
            path = self.files_by_split[split]
            for sent in self.load_data(path):
                yield sent

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process a language modeling split.  Split is a single list of sentences here.  '''
        def _make_instance(input1, input2, labels):
            d = {}
            d["input1"] = _sentence_to_text_field(input1, indexers)
            d["input2"] = _sentence_to_text_field(input2, indexers)
            d["labels"] = LabelField(labels, label_namespace="labels",
                                     skip_indexing=True)
            return Instance(d)
        first = True
        for sent in split:
            if first:
                prev_sent = sent
                first = False
                continue
            yield _make_instance(prev_sent, sent, 1)
            prev_sent = sent

    def count_examples(self):
        ''' Compute here b/c we're streaming the sentences. '''
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            # pair sentence # = sent # - 1
            example_counts[split] = sum(1 for line in open(split_path)) - 1
        self.example_counts = example_counts


@register_task('wiki103_s2s', rel_path='WikiText103/', max_targ_v_size=0)
class Wiki103Seq2SeqTask(MTTask):
    ''' Skipthought objective on Wiki103 '''

    def __init__(self, path, max_seq_len, max_targ_v_size, name='wiki103_mt'):
        ''' Note: max_targ_v_size does nothing here '''
        super().__init__(path, max_seq_len, max_targ_v_size, name)
        # for skip-thoughts setting, all source sentences are sentences that
        # followed by another sentence (which are all but the last one).
        # Similar for self.target_sentences
        self._nonatomic_toks = [UNK_TOK_ALLENNLP, '<unk>']
        self._label_namespace = None
        self.target_indexer = {"words": SingleIdTokenIndexer("tokens")}
        self.files_by_split = {"train": os.path.join(path, "train.sentences.txt"),
                               "val": os.path.join(path, "valid.sentences.txt"),
                               "test": os.path.join(path, "test.sentences.txt")}

    def load_data(self, path):
        ''' Load data '''
        nonatomic_toks = self._nonatomic_toks
        with codecs.open(path, 'r', 'utf-8', errors='ignore') as txt_fh:
            for row in txt_fh:
                toks = row.strip()
                if not toks:
                    continue
                sent = _atomic_tokenize(toks, UNK_TOK_ATOMIC, nonatomic_toks,
                                        self.max_seq_len)
                yield sent, []

    def get_num_examples(self, split_text):
        ''' Return number of examples in the result of get_split_text.

        Subclass can override this if data is not stored in column format.
        '''
        # pair setences# = sent# - 1
        return len(split_text) - 1

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process a language modeling split.

        Split is a single list of sentences here.
        '''
        target_indexer = self.target_indexer

        def _make_instance(prev_sent, sent):
            d = {}
            d["inputs"] = _sentence_to_text_field(prev_sent, indexers)
            d["targs"] = _sentence_to_text_field(sent, target_indexer)
            return Instance(d)

        prev_sent = None
        for sent, _ in split:
            if prev_sent is None:
                prev_sent = sent
                continue
            yield _make_instance(prev_sent, sent)
            prev_sent = sent


@register_task('dissentwiki', rel_path='DisSent/wikitext/')
class DisSentTask(PairClassificationTask):
    ''' Task class for DisSent, dataset agnostic.
        Based on Nie, Bennett, and Goodman (2017), but with different datasets.
    '''

    def __init__(self, path, max_seq_len, prefix, name="dissent"):
        ''' There are 8 classes because there are 8 discourse markers in
            the dataset (and, but, because, if, when, before, though, so)
        '''
        super().__init__(name, 8)
        self.max_seq_len = max_seq_len
        self.files_by_split = {"train": os.path.join(path, "%s.train" % prefix),
                               "val": os.path.join(path, "%s.valid" % prefix),
                               "test": os.path.join(path, "%s.test" % prefix)}

    def get_split_text(self, split: str):
        ''' Get split text as iterable of records.

        Split should be one of 'train', 'val', or 'test'.
        '''
        return self.load_data(self.files_by_split[split])

    def load_data(self, path):
        ''' Load data '''
        with open(path, 'r') as txt_fh:
            for row in txt_fh:
                row = row.strip().split('\t')
                if len(row) != 3 or not (row[0] and row[1] and row[2]):
                    continue
                sent1 = process_sentence(row[0], self.max_seq_len)
                sent2 = process_sentence(row[1], self.max_seq_len)
                targ = int(row[2])
                yield (sent1, sent2, targ)

    def get_sentences(self) -> Iterable[Sequence[str]]:
        ''' Yield sentences, used to compute vocabulary. '''
        for split in self.files_by_split:
            ''' Don't use test set for vocab building. '''
            if split.startswith("test"):
                continue
            path = self.files_by_split[split]
            for sent1, sent2, _ in self.load_data(path):
                yield sent1
                yield sent2

    def count_examples(self):
        ''' Compute the counts here b/c we're streaming the sentences. '''
        example_counts = {}
        for split, split_path in self.files_by_split.items():
            example_counts[split] = sum(1 for line in open(split_path))
        self.example_counts = example_counts

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process split text into a list of AllenNLP Instances. '''
        def _make_instance(input1, input2, labels):
            d = {}
            d["input1"] = _sentence_to_text_field(input1, indexers)
            d["input2"] = _sentence_to_text_field(input2, indexers)
            d["labels"] = LabelField(labels, label_namespace="labels",
                                     skip_indexing=True)
            return Instance(d)

        for sent1, sent2, trg in split:
            yield _make_instance(sent1, sent2, trg)


class DisSentWikiSingleTask(DisSentTask):
    ''' Task class for DisSent with Wikitext 103 only considering clauses from within a single sentence
        Data sets should be prepared as described in Nie, Bennett, and Goodman (2017) '''

    def __init__(self, path, max_seq_len, name="dissentwiki"):
        super().__init__(path, max_seq_len, "wikitext.dissent.single_sent", name)


@register_task('dissentwikifullbig', rel_path='DisSent/wikitext/')
class DisSentWikiBigFullTask(DisSentTask):
    ''' Task class for DisSent with Wikitext 103 considering clauses from within a single sentence
        or across two sentences.
        Data sets should be prepared as described in Nie, Bennett, and Goodman (2017) '''

    def __init__(self, path, max_seq_len, name="dissentwikifullbig"):
        super().__init__(path, max_seq_len, "wikitext.dissent.big", name)


@register_task('weakgrounded', rel_path='mscoco/weakgrounded/')
class WeakGroundedTask(PairClassificationTask):
    ''' Task class for Weak Grounded Sentences i.e., training on pairs of captions for the same image '''

    def __init__(self, path, max_seq_len, n_classes, name="weakgrounded"):
        ''' Do stuff '''
        super(WeakGroundedTask, self).__init__(name, n_classes)

        ''' Process the dataset located at path.  '''
        ''' positive = captions of the same image, negative = captions of different images '''
        targ_map = {'negative': 0, 'positive': 1}
        targ_map = {'0': 0, '1': 1}

        tr_data = load_tsv(os.path.join(path, "train_aug.tsv"), max_seq_len, targ_map=targ_map,
                           s1_idx=0, s2_idx=1, targ_idx=2, skip_rows=0)
        val_data = load_tsv(os.path.join(path, "val.tsv"), max_seq_len, targ_map=targ_map,
                            s1_idx=0, s2_idx=1, targ_idx=2, skip_rows=0)
        te_data = load_tsv(os.path.join(path, "test.tsv"), max_seq_len, targ_map=targ_map,
                           s1_idx=0, s2_idx=1, targ_idx=2, skip_rows=0)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        self.sentences = self.train_data_text[0] + self.val_data_text[0]
        self.n_classes = 2
        log.info("\tFinished loading MSCOCO data.")


@register_task('grounded', rel_path='mscoco/grounded/')
class GroundedTask(Task):
    ''' Task class for Grounded Sentences i.e., training on caption->image pair '''
    ''' Defined new metric function from AllenNLP Average '''

    def __init__(self, path, max_seq_len, name="grounded"):
        ''' Do stuff '''
        super(GroundedTask, self).__init__(name)
        self.scorer1 = Average()
        self.scorer2 = None
        self.val_metric = "%s_metric" % self.name
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + \
            self.val_data_text[0]
        self.ids = self.train_data_text[1] + \
            self.val_data_text[1]
        self.path = path
        self.img_encoder = None
        self.val_metric_decreases = False

    def _compute_metric(self, metric_name, tensor1, tensor2):
        '''Metrics for similarity in image space'''

        np1, np2 = tensor1.data.numpy(), tensor2.data.numpy()

        if metric_name is 'abs_diff':
            metric = np.mean(np1 - np2)
        elif metric_name is 'cos_sim':
            metric = cos_sim(np.asarray(np1), np.asarray(np2))[0][0]
        else:
            print('Undefined metric name!')
            metric = 0

        return metric

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        metric = self.scorer1.get_metric(reset)

        return {'metric': metric}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        '''
        Convert a dataset of sentences into padded sequences of indices.
        Args:
            - split (list[list[str]]): list of inputs (possibly pair) and outputs
            - pair_input (int)
            - tok2idx (dict)
        Returns:
        '''
        def _make_instance(sent, label, ids):
            input1 = _sentence_to_text_field(sent, indexers)
            label = NumericField(label)
            ids = NumericField(ids)
            return Instance({"input1": input1, "labels": label, "ids": ids})

        # Map over columns: input1, labels, ids
        instances = map(_make_instance, *split)
        #  return list(instances)
        return instances  # lazy iterator

    def load_data(self, path, max_seq_len):
        ''' Map sentences to image ids
            Keep track of caption ids just in case '''

        train, val, test = ([], [], []), ([], [], []), ([], [], [])

        with open(os.path.join(path, "train_idx.txt"), 'r') as f:
            train_ids = [item.strip() for item in f.readlines()]
        with open(os.path.join(path, "val_idx.txt"), 'r') as f:
            val_ids = [item.strip() for item in f.readlines()]
        with open(os.path.join(path, "test_idx.txt"), 'r') as f:
            test_ids = [item.strip() for item in f.readlines()]

        f = open(os.path.join(path, "train.json"), 'r')
        for line in f:
            tr_dict = json.loads(line)
        f = open(os.path.join(path, "val.json"), 'r')
        for line in f:
            val_dict = json.loads(line)
        f = open(os.path.join(path, "test.json"), 'r')
        for line in f:
            te_dict = json.loads(line)
        with open(os.path.join(path, "feat_map.json")) as fd:
            keymap = json.load(fd)

        def load_mscoco(data_dict, data_list, img_idxs):
            for img_idx in img_idxs:
                newimg_id = 'mscoco/grounded/' + img_idx + '.json'
                for caption_id in data_dict[img_idx]['captions']:
                    data_list[0].append(data_dict[img_idx]['captions'][caption_id])
                    data_list[1].append(1)
                    data_list[2].append(int(keymap[newimg_id]))
            return data_list

        train = load_mscoco(tr_dict, train, train_ids)
        val = load_mscoco(val_dict, val, val_ids)
        test = load_mscoco(te_dict, test, test_ids)

        self.tr_data = train
        self.val_data = val
        self.te_data = test
        self.train_data_text = train
        self.val_data_text = val
        self.test_data_text = test

        log.info("\tTrain: %d, Val: %d, Test: %d", len(train[0]), len(val[0]), len(test[0]))
        log.info("\tFinished loading MSCOCO data!")


@register_task('groundedsw', rel_path='mscoco/grounded')
class GroundedSWTask(Task):
    ''' Task class for Grounded Sentences i.e., training on caption->image pair '''
    ''' Defined new metric function from AllenNLP Average '''

    def __init__(self, path, max_seq_len, name="groundedsw"):
        ''' Do stuff '''
        super(GroundedSWTask, self).__init__(name)
        self.scorer1 = Average()
        self.scorer2 = None
        self.val_metric = "%s_metric" % self.name
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + \
            self.val_data_text[0]
        self.ids = self.train_data_text[1] + \
            self.val_data_text[1]
        self.path = path
        self.img_encoder = None
        self.val_metric_decreases = False

    def _compute_metric(self, metric_name, tensor1, tensor2):
        '''Metrics for similarity in image space'''

        np1, np2 = tensor1.data.numpy(), tensor2.data.numpy()

        if metric_name is 'abs_diff':
            metric = np.mean(np1 - np2)
        elif metric_name is 'cos_sim':
            metric = cos_sim(np.asarray(np1), np.asarray(np2))[0][0]
        else:
            log.warning('Undefined metric name!')
            metric = 0

        return metric

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        metric = self.scorer1.get_metric(reset)

        return {'metric': metric}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        '''
        Convert a dataset of sentences into padded sequences of indices.
        Args:
            - split (list[list[str]]): list of inputs (possibly pair) and outputs
            - pair_input (int)
            - tok2idx (dict)
        Returns:
        '''
        def _make_instance(sent, label, ids):
            input1 = _sentence_to_text_field(sent, indexers)
            label = NumericField(label)
            ids = NumericField(ids)
            return Instance({"input1": input1, "labels": label, "ids": ids})

        # Map over columns: input1, labels, ids
        instances = map(_make_instance, *split)
        #  return list(instances)
        return instances  # lazy iterator

    def load_data(self, path, max_seq_len):
        ''' Map sentences to image ids
            Keep track of caption ids just in case '''

        train, val, test = ([], [], []), ([], [], []), ([], [], [])

        def get_data(dataset, data):
            f = open(path + dataset + ".tsv", 'r')
            for line in f:
                items = line.strip().split('\t')
                if len(items) < 3 or items[1] == '0':
                    continue
                data[0].append(items[0])
                data[1].append(int(items[1]))
                data[2].append(int(items[2]))
            return data

        train = get_data('shapeworld/train', train)
        val = get_data('shapeworld/val', val)
        test = get_data('shapeworld/test', test)

        self.tr_data = train
        self.val_data = val
        self.te_data = test
        self.train_data_text = train
        self.val_data_text = val
        self.test_data_text = test

        log.info("Train: %d, Val: %d, Test: %d", len(train[0]), len(val[0]), len(test[0]))
        log.info("\nFinished loading SW data!")


class RecastNLITask(PairClassificationTask):
    ''' Task class for NLI Recast Data'''

    def __init__(self, path, max_seq_len, name="recast"):
        super(RecastNLITask, self).__init__(name, 2)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.train_data_text[1] + \
            self.val_data_text[0] + self.val_data_text[1]

    def load_data(self, path, max_seq_len):
        tr_data = load_tsv(os.path.join(path, 'train.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, skip_rows=0, targ_idx=3)
        val_data = load_tsv(os.path.join(path, 'dev.tsv'), max_seq_len,
                            s1_idx=0, s2_idx=1, skip_rows=0, targ_idx=3)
        te_data = load_tsv(os.path.join(path, 'test.tsv'), max_seq_len,
                           s1_idx=1, s2_idx=2, skip_rows=0, targ_idx=3)

        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading recast probing data.")


@register_task('recast-puns', rel_path='DNC/recast_puns_data')
class RecastPunTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-puns"):
        super(RecastPunTask, self).__init__(path, max_seq_len, name)


@register_task('recast-ner', rel_path='DNC/recast_ner_data')
class RecastNERTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-ner"):
        super(RecastNERTask, self).__init__(path, max_seq_len, name)


@register_task('recast-verbnet', rel_path='DNC/recast_verbnet_data')
class RecastVerbnetTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-verbnet"):
        super(RecastVerbnetTask, self).__init__(path, max_seq_len, name)


@register_task('recast-verbcorner', rel_path='DNC/recast_verbcorner_data')
class RecastVerbcornerTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-verbcorner"):
        super(RecastVerbcornerTask, self).__init__(path, max_seq_len, name)


@register_task('recast-sentiment', rel_path='DNC/recast_sentiment_data')
class RecastSentimentTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-sentiment"):
        super(RecastSentimentTask, self).__init__(path, max_seq_len, name)


@register_task('recast-factuality', rel_path='DNC/recast_factuality_data')
class RecastFactualityTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-factuality"):
        super(RecastFactualityTask, self).__init__(path, max_seq_len, name)


@register_task('recast-winogender', rel_path='DNC/manually-recast-winogender')
class RecastWinogenderTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-winogender"):
        super(RecastWinogenderTask, self).__init__(path, max_seq_len, name)


@register_task('recast-lexicosyntax', rel_path='DNC/lexicosyntactic_recasted')
class RecastLexicosynTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-lexicosyn"):
        super(RecastLexicosynTask, self).__init__(path, max_seq_len, name)


@register_task('recast-kg', rel_path='DNC/kg-relations')
class RecastKGTask(RecastNLITask):

    def __init__(self, path, max_seq_len, name="recast-kg"):
        super(RecastKGTask, self).__init__(path, max_seq_len, name)


class TaggingTask(Task):
    ''' Generic tagging task, one tag per word '''

    def __init__(self, name, num_tags):
        super().__init__(name)
        self.num_tags = num_tags + 2  # add tags for unknown and padding
        self.scorer1 = CategoricalAccuracy()
        self.val_metric = "%s_accuracy" % self.name
        self.val_metric_decreases = False
        self.all_labels = [str(i) for i in range(self.num_tags)]
        self._label_namespace = self.name + "_tags"
        self.target_indexer = {"words": SingleIdTokenIndexer(namespace=self._label_namespace)}

    def truncate(self, max_seq_len, sos_tok="<SOS>", eos_tok="<EOS>"):
        ''' Truncate the data if any sentences are longer than max_seq_len. '''
        self.train_data_text = [truncate(self.train_data_text[0], max_seq_len,
                                         sos_tok, eos_tok), self.train_data_text[1]]
        self.val_data_text = [truncate(self.val_data_text[0], max_seq_len,
                                       sos_tok, eos_tok), self.val_data_text[1]]
        self.test_data_text = [truncate(self.test_data_text[0], max_seq_len,
                                        sos_tok, eos_tok), self.test_data_text[1]]

    def get_metrics(self, reset=False):
        '''Get metrics specific to the task'''
        acc = self.scorer1.get_metric(reset)
        return {'accuracy': acc}

    def process_split(self, split, indexers) -> Iterable[Type[Instance]]:
        ''' Process a tagging task '''
        inputs = [TextField(list(map(Token, sent)), token_indexers=indexers) for sent in split[0]]
        targs = [TextField(list(map(Token, sent)), token_indexers=self.target_indexer)
                 for sent in split[2]]
        # Might be better as LabelField? I don't know what these things mean
        instances = [Instance({"inputs": x, "targs": t}) for (x, t) in zip(inputs, targs)]
        return instances

    def get_all_labels(self) -> List[str]:
        return self.all_labels


@register_task('ccg', rel_path='CCG/')
class CCGTaggingTask(TaggingTask):
    ''' CCG supertagging as a task.
        Using the supertags from CCGbank. '''

    def __init__(self, path, max_seq_len, name="ccg"):
        ''' There are 1363 supertags in CCGBank. '''
        super().__init__(name, 1363)
        self.load_data(path, max_seq_len)
        self.sentences = self.train_data_text[0] + self.val_data_text[0]

    def load_data(self, path, max_seq_len):
        '''Process the dataset located at each data file.
           The target needs to be split into tokens because
           it is a sequence (one tag per input token). '''
        tr_data = load_tsv(os.path.join(path, "ccg_1363.train"), max_seq_len,
                           s1_idx=0, s2_idx=None, targ_idx=1, targ_fn=lambda t: t.split(' '))
        val_data = load_tsv(os.path.join(path, "ccg_1363.dev"), max_seq_len,
                            s1_idx=0, s2_idx=None, targ_idx=1, targ_fn=lambda t: t.split(' '))
        te_data = load_tsv(os.path.join(path, 'ccg_1363.test'), max_seq_len,
                           s1_idx=0, s2_idx=None, targ_idx=1, targ_fn=lambda t: t.split(' '))
        self.train_data_text = tr_data
        self.val_data_text = val_data
        self.test_data_text = te_data
        log.info("\tFinished loading CCGTagging data.")
