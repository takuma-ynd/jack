# -*- coding: utf-8 -*-

import argparse
import os
import os.path as path

from time import time
import sys

import logging

import tensorflow as tf

from jtr.jack import load_labelled_data
from jtr.jack.train.hooks import EvalHook, XQAEvalHook, LossHook
from jtr.preprocess.batch import get_feed_dicts
from jtr.preprocess.vocab import Vocab
from jtr.train import train
import jtr.jack.readers as readers
from jtr.load.embeddings.embeddings import load_embeddings
from jtr.pipelines import create_placeholders, pipeline

from jtr.load.read_jtr import jtr_load as _jtr_load
from tensorflow.python.client import device_lib

logger = logging.getLogger(os.path.basename(sys.argv[0]))


class Duration(object):
    def __init__(self):
        self.t0 = time()
        self.t = time()

    def __call__(self):
        logger.info('Time since last checkpoint : {0:.2g}min'.format((time()-self.t)/60.))
        self.t = time()

tf.set_random_seed(1337)
checkpoint = Duration()


def main():
    # Please add new models to readers when they work

    support_alts = {'none', 'single', 'multiple'}
    question_alts = answer_alts = {'single', 'multiple'}
    candidate_alts = {'open', 'per-instance', 'fixed'}

    train_default = dev_default = test_default = 'tests/test_data/sentihood/overfit.json'

    parser = argparse.ArgumentParser(description='Train and Evaluate a Machine Reader')
    parser.add_argument('--debug', action='store_true',
                        help="Run in debug mode, in which case the training file is also used for testing")

    parser.add_argument('--debug_examples', default=10, type=int,
                        help="If in debug mode, how many examples should be used (default 2000)")
    parser.add_argument('--train', default=train_default, type=str, help="jtr training file")
    parser.add_argument('--dev', default=dev_default, type=str, help="jtr dev file")
    parser.add_argument('--test', default=test_default, type=str, help="jtr test file")
    parser.add_argument('--supports', default='single', choices=sorted(support_alts),
                        help="None, single (default) or multiple supporting statements per instance; multiple_flat reads multiple instances creates a separate instance for every support")
    parser.add_argument('--questions', default='single', choices=sorted(question_alts),
                        help="None, single (default), or multiple questions per instance")
    parser.add_argument('--candidates', default='fixed', choices=sorted(candidate_alts),
                        help="Open, per-instance, or fixed (default) candidates")
    parser.add_argument('--answers', default='single', choices=sorted(answer_alts),
                        help="Single or multiple")
    parser.add_argument('--batch_size', default=128,
        type=int, help="Batch size for training data, default 128")
    parser.add_argument('--dev_batch_size', default=128,
        type=int, help="Batch size for development data, default 128")
    parser.add_argument('--repr_dim_input', default=100, type=int,
                        help="Size of the input representation (embeddings), default 100 (embeddings cut off or extended if not matched with pretrained embeddings)")
    parser.add_argument('--repr_dim', default=100, type=int,
                        help="Size of the hidden representations, default 100")

    parser.add_argument('--pretrain', action='store_true',
                        help="Use pretrained embeddings, by default the initialisation is random")
    parser.add_argument('--vocab_from_embeddings', action='store_true',
                        help="Use fixed vocab of pretrained embeddings")
    parser.add_argument('--train_pretrain', action='store_true',
                        help="Continue training pretrained embeddings together with model parameters")
    parser.add_argument('--normalize_pretrain', action='store_true',
                        help="Normalize pretrained embeddings, default True (randomly initialized embeddings have expected unit norm too)")

    parser.add_argument('--vocab_maxsize', default=sys.maxsize, type=int)
    parser.add_argument('--vocab_minfreq', default=2, type=int)
    parser.add_argument('--vocab_sep', default=True, type=bool, help='Should there be separate vocabularies for questions, supports, candidates and answers. This needs to be set to True for candidate-based methods.')
    parser.add_argument('--model', default='fastqa_reader', choices=sorted(readers.readers.keys()), help="Reading model to use")
    parser.add_argument('--learning_rate', default=0.001, type=float, help="Learning rate, default 0.001")
    parser.add_argument('--learning_rate_decay', default=0.5, type=float, help="Learning rate decay, default 0.5")
    parser.add_argument('--l2', default=0.0, type=float, help="L2 regularization weight, default 0.0")
    parser.add_argument('--clip_value', default=0.0, type=float,
                        help="Gradients clipped between [-clip_value, clip_value] (default 0.0; no clipping)")
    parser.add_argument('--dropout', default=0.0, type=float,
                        help="Probability for dropout on output (set to 0.0 for no dropout)")
    parser.add_argument('--epochs', default=5, type=int, help="Number of epochs to train for, default 5")

    parser.add_argument('--negsamples', default=0, type=int,
                        help="Number of negative samples, default 0 (= use full candidate list)")
    parser.add_argument('--tensorboard_folder', default='./.tb/',
                        help='Folder for tensorboard logs')
    parser.add_argument('--write_metrics_to', default='',
                        help='Filename to log the metrics of the EvalHooks')
    parser.add_argument('--prune', default='False',
                        help='If the vocabulary should be pruned to the most frequent words.')
    parser.add_argument('--model_dir', default=None, type=str, help="Directory to write reader to.")

    args = parser.parse_args()

    clip_value = None
    if args.clip_value != 0.0:
        clip_value = - abs(args.clip_value), abs(args.clip_value)

    logger.info('configuration:')
    for arg in vars(args):
        logger.info('\t{} : {}'.format(str(arg), str(getattr(args, arg))))

    # Get information about available CPUs and GPUs:
    # to set specific device, add CUDA_VISIBLE_DEVICES environment variable, e.g.
    # $ CUDA_VISIBLE_DEVICES=0 ./jtr_script.py

    logger.info('available devices:')
    for l in device_lib.list_local_devices():
        logger.info('device info: ' + str(l).replace("\n", " "))

    # (3) Read the train, dev, and test data (with optionally loading pretrained embeddings)

    embeddings = None
    if args.debug:
        train_data = load_labelled_data(args.train, args.debug_examples, **vars(args))

        logger.info('loaded {} samples as debug train/dev/test dataset '.format(args.debug_examples))

        dev_data = train_data
        test_data = train_data
        if args.pretrain:
            emb_file = 'glove.6B.50d.txt'
            embeddings = load_embeddings(path.join('jtr', 'data', 'GloVe', emb_file), 'glove')
            logger.info('loaded pre-trained embeddings ({})'.format(emb_file))
            args.repr_input_dim = embeddings.lookup.shape[1]
    else:
        train_data, dev_data, test_data = [load_labelled_data(name, **vars(args)) for name in [args.train, args.dev, args.test]]
        logger.info('loaded train/dev/test data')
        if args.pretrain:
            #TODO: add options for other embeddings
            emb_file = 'GoogleNews-vectors-negative300.bin.gz'
            embeddings = load_embeddings(path.join('jtr', 'data', 'SG_GoogleNews', emb_file), 'word2vec')
            logger.info('loaded pre-trained embeddings ({})'.format(emb_file))
            args.repr_input_dim = embeddings.lookup.shape[1]

    emb = embeddings if args.pretrain else None

    vocab = Vocab(emb=emb, init_from_embeddings=args.vocab_from_embeddings)

    learning_rate = tf.get_variable("learning_rate", initializer=args.learning_rate, dtype=tf.float32, trainable=False)
    optim = tf.train.AdamOptimizer(learning_rate)
    sw = tf.summary.FileWriter(args.tensorboard_folder)

    # build JTReader
    checkpoint()
    reader = readers.readers[args.model](vocab, vars(args))
    checkpoint()

    # Hooks
    hooks = [LossHook(reader, 1 if args.debug else 100, summary_writer=sw)]

    preferred_metric = "f1"  #TODO: this should depend on the task, for now I set it to 1
    def side_effect(metrics, prev_metric):
        """Returns: a state (in this case a metric) that is used as input for the next call"""
        m = metrics[preferred_metric]
        if prev_metric is not None and m < prev_metric:
            reader.sess.run(lr_decay_op)
            logger.info("Decayed learning rate to: %.5f" % reader.sess.run(learning_rate))
        else:
            reader.store(args.model_dir)
            logger.info("Saving model to: %s" % args.model_dir)
        return m

    if args.model in readers.xqa_readers:
        lr_decay_op = learning_rate.assign(args.learning_rate_decay * learning_rate)
        hooks.append(XQAEvalHook(reader, dev_data, summary_writer=sw, side_effect=side_effect))
    if args.model in readers.genqa_readers:
        pass
    if args.model in readers.mcqa_readers:
        pass

    # Train
    reader.train(optim, training_set=train_data,
                 max_epochs=args.epochs, hooks=hooks,
                 l2=args.l2, clip=clip_value, clip_op=tf.clip_by_value)

    if args.debug:
        with tf.Graph().as_default():
            print("Test loading of reader.")
            # Load model to ensure loading works
            reader.setup_from_file(args.model_dir)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
