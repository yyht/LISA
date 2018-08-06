import tensorflow as tf
from tensorflow.estimator import ModeKeys
import numpy as np
import constants
import evaluation_fns
import output_fns
import transformer
import nn_utils
import train_utils
from lazy_adam_v2 import LazyAdamOptimizer


class LISAModel:

  def __init__(self, hparams, model_config, task_config, feature_idx_map, label_idx_map, vocab):
    self.hparams = hparams
    self.model_config = model_config
    self.task_config = task_config
    self.feature_idx_map = feature_idx_map
    self.label_idx_map = label_idx_map
    self.vocab = vocab

  def load_transitions(self, transition_statistics, num_classes, vocab_map):
    transition_statistics_np = np.zeros((num_classes, num_classes))
    with open(transition_statistics, 'r') as f:
      for line in f:
        tag1, tag2, prob = line.split("\t")
        transition_statistics_np[vocab_map[tag1], vocab_map[tag2]] = float(prob)
    return transition_statistics_np

  def get_embedding_lookup(self, name, embedding_dim, embedding_values, include_oov,
                           pretrained_fname=None, num_embeddings=None):

    with tf.variable_scope("%s_embeddings" % name):
      initializer = tf.random_normal_initializer()
      if pretrained_fname:
        pretrained_embeddings = self.load_pretrained_embeddings(pretrained_fname)
        initializer = tf.constant_initializer(pretrained_embeddings)
        pretrained_num_embeddings, pretrained_embedding_dim = pretrained_embeddings.shape
        if pretrained_embedding_dim != embedding_dim:
          tf.logging.log(tf.logging.ERROR, "Pre-trained %s embedding dim does not match"
                                           " specified dim (%d vs %d)." % (name,
                                                                           pretrained_embedding_dim,
                                                                           embedding_dim))
        if num_embeddings and num_embeddings != pretrained_num_embeddings:
          tf.logging.log(tf.logging.ERROR, "Number of pre-trained %s embeddings does not match"
                                           " specified number of embeddings (%d vs %d)." % (name,
                                                                                            pretrained_num_embeddings,
                                                                                            num_embeddings))
        num_embeddings = pretrained_num_embeddings

      embedding_table = tf.get_variable(name="embeddings", shape=[num_embeddings, embedding_dim],
                                        initializer=initializer)

      if include_oov:
        oov_embedding = tf.get_variable(name="oov_embedding", shape=[1, embedding_dim],
                                        initializer=tf.random_normal_initializer())
        embedding_table = tf.concat([embedding_table, oov_embedding], axis=0,
                                    name="embeddings_table")

      return tf.nn.embedding_lookup(embedding_table, embedding_values)

  def load_pretrained_embeddings(self, pretrained_fname):
    tf.logging.log(tf.logging.INFO, "Loading pre-trained embedding file: %s" % pretrained_fname)

    # TODO: np.loadtxt refuses to work for some reason
    # pretrained_embeddings = np.loadtxt(self.args.word_embedding_file, usecols=range(1, word_embedding_size+1))
    pretrained_embeddings = []
    with open(pretrained_fname, 'r') as f:
      for line in f:
        split_line = line.split()
        embedding = list(map(float, split_line[1:]))
        pretrained_embeddings.append(embedding)
    pretrained_embeddings = np.array(pretrained_embeddings)
    pretrained_embeddings /= np.std(pretrained_embeddings)
    return pretrained_embeddings

  def model_fn(self, features, mode):

    if mode != ModeKeys.TRAIN:
      for hparam in self.hparams:
        if 'dropout' in hparam:
          self.hparams[hparam] = 1.0

    # todo move this somewhere else?
    moving_averager = tf.train.ExponentialMovingAverage(self.hparams.moving_average_decay)
    moving_average_op = moving_averager.apply(tf.trainable_variables())
    tf.add_to_collection(tf.GraphKeys.UPDATE_OPS, moving_average_op)

    def moving_average_getter(getter, name, *args, **kwargs):
      var = getter(name, *args, **kwargs)
      averaged_var = moving_averager.average(var)
      return averaged_var if averaged_var else var

    getter = None if mode == ModeKeys.TRAIN else moving_average_getter

    with tf.variable_scope("LISA", reuse=tf.AUTO_REUSE, custom_getter=getter):

      batch_shape = tf.shape(features)
      batch_size = batch_shape[0]
      batch_seq_len = batch_shape[1]
      layer_config = self.model_config['layers']
      sa_hidden_size = layer_config['head_dim'] * layer_config['num_heads']

      feats = {f: features[:, :, idx] for f, idx in self.feature_idx_map.items()}

      # todo this assumes that word_type is always passed in
      words = feats['word_type']

      # for masking out padding tokens
      tokens_to_keep = tf.where(tf.equal(words, constants.PAD_VALUE), tf.zeros([batch_size, batch_seq_len]),
                                tf.ones([batch_size, batch_seq_len]))

      # todo fix masking -- do it in lookup table?
      feats = {f: tf.multiply(tf.cast(tokens_to_keep, tf.int32), v) for f, v in feats.items()}

      labels = {}
      for l, idx in self.label_idx_map.items():
        these_labels = features[:, :, idx[0]:idx[1]] if idx[1] != -1 else features[:, :, idx[0]:]
        these_labels_masked = tf.multiply(these_labels, tf.cast(tf.expand_dims(tokens_to_keep, -1), tf.int32))
        # check if we need to mask another dimension
        if idx[1] == -1:
          last_dim = tf.shape(these_labels)[2]
          this_mask = tf.where(tf.equal(these_labels_masked, constants.PAD_VALUE),
                               tf.zeros([batch_size, batch_seq_len, last_dim], dtype=tf.int32),
                               tf.ones([batch_size, batch_seq_len, last_dim], dtype=tf.int32))
          these_labels_masked = tf.multiply(these_labels_masked, this_mask)
        else:
          these_labels_masked = tf.squeeze(these_labels_masked, -1)
        labels[l] = these_labels_masked

      inputs_list = []
      for input_name, input_map in self.model_config['inputs'].items():
        # words = feats['word_type']
        input_values = feats[input_name]
        input_embedding_dim = input_map['embedding_dim']
        if 'pretrained_embeddings' in input_map:
          input_pretrained_embeddings = input_map['pretrained_embeddings']
          input_include_oov = True
          input_embedding_lookup = self.get_embedding_lookup(input_name, input_embedding_dim,
                                                             input_values, input_include_oov,
                                                             pretrained_fname=input_pretrained_embeddings)
        else:
          num_embeddings = self.vocab.vocab_names_sizes[input_name]
          input_include_oov = self.vocab.oovs[input_name]
          input_embedding_lookup = self.get_embedding_lookup(input_name, input_embedding_dim,
                                                             input_values, input_include_oov,
                                                             num_embeddings=num_embeddings)
        inputs_list.append(input_embedding_lookup)
        tf.logging.log(tf.logging.INFO, "Added %s to inputs list." % input_name)

      current_input = tf.concat(inputs_list, axis=2)

      # todo this is parse specific
      # compute targets adj matrix
      # i1, i2 = tf.meshgrid(tf.range(batch_size), tf.range(batch_seq_len), indexing="ij")
      # idx = tf.stack([i1, i2, labels['parse_head']], axis=-1)
      # adj = tf.scatter_nd(idx, tf.ones([batch_size, batch_seq_len]), [batch_size, batch_seq_len, batch_seq_len])
      # mask2d = tokens_to_keep * tf.transpose(tokens_to_keep)
      # adj = adj * mask2d

      # word_embeddings_table = self.load_pretrained_embeddings(self.args.word_embedding_file)
      # word_embeddings = tf.nn.embedding_lookup(word_embeddings_table, words)
      # current_input = word_embeddings

      # todo will estimators handle dropout for us or do we need to do it on our own?
      input_dropout = self.hparams.input_dropout
      current_input = tf.nn.dropout(current_input, input_dropout if mode == tf.estimator.ModeKeys.TRAIN else 1.0)

      with tf.variable_scope('project_input'):
        current_input = nn_utils.MLP(current_input, sa_hidden_size, n_splits=1)

      manual_attn = None
      predictions = {}
      eval_metric_ops = {}
      export_outputs = {}
      loss = tf.constant(0.)
      items_to_log = {}

      num_layers = max(self.task_config.keys()) + 1
      tf.logging.log(tf.logging.INFO, "Creating transformer model with %d layers" % num_layers)
      with tf.variable_scope('transformer'):
        current_input = transformer.add_timing_signal_1d(current_input)
        for i in range(num_layers):
          with tf.variable_scope('layer%d' % i):
            current_input = transformer.transformer(mode, current_input, tokens_to_keep, layer_config['head_dim'],
                                                    layer_config['num_heads'], layer_config['attn_dropout'],
                                                    layer_config['ff_dropout'], layer_config['prepost_dropout'],
                                                    layer_config['ff_hidden_size'],
                                                    manual_attn)
            if i in self.task_config:

              # if normalization is done in layer_preprocess, then it should also be done
              # on the output, since the output can grow very large, being the sum of
              # a whole stack of unnormalized layer outputs.
              current_input = nn_utils.layer_norm(current_input)

              # todo test a list of tasks for each layer
              for task, task_map in self.task_config[i].items():
                task_labels = labels[task]
                task_vocab_size = self.vocab.vocab_names_sizes[task]

                # Set up CRF / Viterbi transition params if specified
                with tf.variable_scope("crf"):  # to share parameters, change scope here
                  transition_stats_file = task_map['transition_stats'] if 'transition_stats' in task_map else None

                  # todo vocab_lookups not yet initialized -- fix
                  transition_stats = self.load_transitions(transition_stats_file, task_vocab_size,
                                                           self.vocab.vocab_maps[task]) if transition_stats_file else None

                  # create transition parameters if training or decoding with crf/viterbi
                  task_crf = 'crf' in task_map and task_map['crf']
                  task_viterbi_decode = task_crf or 'viterbi' in task_map and task_map['viterbi']
                  transition_params = None
                  if task_viterbi_decode or task_crf:
                    transition_params = tf.get_variable("transitions", [task_vocab_size, task_vocab_size],
                                                        initializer=tf.constant_initializer(transition_stats),
                                                        trainable=task_crf)
                    train_or_decode_str = "training" if task_crf else "decoding"
                    tf.logging.log(tf.logging.INFO, "Created transition params for %s %s" % (train_or_decode_str, task))

                output_fn_params = output_fns.get_params(mode, self.model_config, task_map['output_fn'], predictions,
                                                         feats, labels, current_input, task_labels, task_vocab_size,
                                                         self.vocab.joint_label_lookup_maps, tokens_to_keep,
                                                         transition_params, self.hparams)
                task_outputs = output_fns.dispatch(task_map['output_fn']['name'])(**output_fn_params)

                # want task_outputs to have:
                # - predictions
                # - loss
                # - scores
                predictions[task] = task_outputs

                # do the evaluation
                for eval_name, eval_map in task_map['eval_fns'].items():
                  eval_fn_params = evaluation_fns.get_params(task_outputs, eval_map, predictions, feats, labels,
                                                             task_labels, self.vocab.reverse_maps, tokens_to_keep)
                  eval_result = evaluation_fns.dispatch(eval_map['name'])(**eval_fn_params)
                  eval_metric_ops[eval_name] = eval_result

                # get the individual task loss and apply penalty
                this_task_loss = task_outputs['loss'] * task_map['penalty']

                # log this task's loss
                items_to_log['%s_loss' % task] = this_task_loss

                # add this loss to the overall loss being minimized
                loss += this_task_loss

                # todo add the predictions to export_outputs
                # todo add un-joint predictions too?
                # predict_output = tf.estimator.export.PredictOutput({'scores': task_outputs['scores'],
                #                                                     'predictions': task_outputs['predictions']})
                # export_outputs['%s_predict' % task] = predict_output

      items_to_log['loss'] = loss

      # get learning rate w/ decay
      this_step_lr = train_utils.learning_rate(self.hparams, tf.train.get_global_step())
      items_to_log['lr'] = this_step_lr

      # optimizer = tf.contrib.opt.NadamOptimizer(learning_rate=this_step_lr, beta1=self.hparams.beta1,
      #                                              beta2=self.hparams.beta2, epsilon=self.hparams.epsilon)
      optimizer = LazyAdamOptimizer(learning_rate=this_step_lr, beta1=self.hparams.beta1,
                                    beta2=self.hparams.beta2, epsilon=self.hparams.epsilon,
                                    use_nesterov=self.hparams.use_nesterov)
      gradients, variables = zip(*optimizer.compute_gradients(loss))
      gradients, _ = tf.clip_by_global_norm(gradients, self.hparams.gradient_clip_norm)
      train_op = optimizer.apply_gradients(zip(gradients, variables), global_step=tf.train.get_global_step())

      # export_outputs = {'predict_output': tf.estimator.export.PredictOutput({'scores': scores, 'preds': preds})}

      logging_hook = tf.train.LoggingTensorHook(items_to_log, every_n_iter=20)

      # need to flatten the dict of predictions to make Estimator happy
      flat_predictions = {"%s_%s" % (k1, k2): v2 for k1, v1 in predictions.items() for k2, v2 in v1.items()}

      export_outputs = {tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
                        tf.estimator.export.PredictOutput(flat_predictions)}

      return tf.estimator.EstimatorSpec(mode, flat_predictions, loss, train_op, eval_metric_ops,
                                        training_hooks=[logging_hook], export_outputs=export_outputs)
