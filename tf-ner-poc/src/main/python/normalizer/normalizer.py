#
#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.
#
import os
import re
import zipfile
from tempfile import TemporaryDirectory

import tensorflow as tf
import numpy as np
import random
from math import floor
import sys

def load_data(file):
    with open(file, encoding="utf-8") as f:
        target = []
        source = []
        for line in f:
            parts = re.split(r'\t+', line)
            target.append(parts[0].strip());
            source.append(parts[1].strip())
    return source, target

def encode_name(char_dict, names):

    max_length = 0
    for name in names:
        length = len(name)
        if length > max_length:
            max_length = length

    # TODO: To be able to use padding for variable length sequences
    #       pad with the eos marker

    encoded_names = np.zeros((len(names), max_length))

    for bi in range(len(names)):
        for ci in range(len(names[bi])):
            encoded_names.itemset((bi, ci), char_dict[names[bi][ci]])

    return encoded_names

def mini_batch(target_char_dict, target, source_char_dict, source, batch_size, batch_index):

    begin = batch_index
    end = min(batch_index + batch_size, len(source))

    target_batch = target[begin : end]

    target_length = []
    for i in range(begin, end):
        target_length.append(len(target[i]) + 1) # TODO: The correction should be done in the graph ...

    source_batch = source[batch_index : batch_index + batch_size]
    source_length = []
    for i in range(begin, end):
        source_length.append(len(source[i]))

    return encode_name(target_char_dict, target_batch), np.asarray(target_length), \
           encode_name(source_char_dict, source_batch), np.asarray(source_length)

def create_graph(mode, batch_size, encoder_nchars, max_target_length, decoder_nchars):

    # Hyper  parameters
    encoder_char_dim = 100
    num_units = 256

    batch_size_ph = tf.placeholder_with_default(batch_size, shape=(), name="batch_size")

    # Encoder
    encoder_char_ids_ph = tf.placeholder(tf.int32, shape=[None, None], name="encoder_char_ids")
    encoder_lengths_ph = tf.placeholder(tf.int32, shape=[None], name="encoder_lengths")

    encoder_embedding_weights = tf.get_variable(name="char_embeddings", dtype=tf.float32,
                        shape=[encoder_nchars, encoder_char_dim])

    encoder_emb_inp = tf.nn.embedding_lookup(encoder_embedding_weights, encoder_char_ids_ph)

    if "TRAIN" == mode:
        encoder_emb_inp = tf.nn.dropout(encoder_emb_inp, 0.7)

    encoder_emb_inp = tf.transpose(encoder_emb_inp, perm=[1, 0, 2])

    encoder_cell = tf.nn.rnn_cell.LSTMCell(num_units)
    initial_state = encoder_cell.zero_state(batch_size_ph, dtype=tf.float32)

    encoder_outputs, encoder_state = tf.nn.dynamic_rnn(
        encoder_cell, encoder_emb_inp, initial_state=initial_state,
        sequence_length=encoder_lengths_ph,
        time_major=True, swap_memory=True)

    # Decoder
    decoder_char_ids_ph = tf.placeholder(tf.int32, shape=[None, None], name="decoder_char_ids")
    decoder_lengths = tf.placeholder(tf.int32, shape=[None], name="decoder_lengths")

    # decoder output (decoder_input shifted to the left by one)

    decoder_char_dim = 100
    decoder_embedding_weights = tf.get_variable(name="decoder_char_embeddings", dtype=tf.float32,
                                             shape=[decoder_nchars, decoder_char_dim])

    projection_layer = tf.layers.Dense(units=decoder_nchars, use_bias=True) # To predict one output char at a time ...

    attention_states = tf.transpose(encoder_outputs, [1, 0, 2])

    attention_mechanism = tf.contrib.seq2seq.LuongAttention(
        num_units, attention_states,
        memory_sequence_length=encoder_lengths_ph)

    decoder_cell = tf.nn.rnn_cell.LSTMCell(num_units)

    decoder_cell = tf.contrib.seq2seq.AttentionWrapper(decoder_cell, attention_mechanism,
        attention_layer_size=num_units)

    # decoder_initial_state = encoder_state
    decoder_initial_state = decoder_cell.zero_state(dtype=tf.float32, batch_size=batch_size_ph)

    if "TRAIN" == mode:

        decoder_input = tf.pad(decoder_char_ids_ph, tf.constant([[0,0], [1,0]]),
                               'CONSTANT', constant_values=(decoder_nchars-2))

        decoder_emb_inp = tf.nn.embedding_lookup(decoder_embedding_weights, decoder_input)
        decoder_emb_inp = tf.transpose(decoder_emb_inp, perm=[1, 0, 2])

        helper = tf.contrib.seq2seq.TrainingHelper(
            decoder_emb_inp, [max_target_length for _ in range(batch_size)], time_major=True)


        decoder = tf.contrib.seq2seq.BasicDecoder(decoder_cell, helper,
                                                  decoder_initial_state, output_layer=projection_layer)

        outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder, output_time_major=True, swap_memory=True )

        logits = outputs.rnn_output
        train_prediction = outputs.sample_id

        decoder_output = tf.pad(tf.transpose(decoder_char_ids_ph, perm=[1, 0]), tf.constant([[0,1], [0,0]]),
                                'CONSTANT', constant_values=(decoder_nchars-1))

        crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=decoder_output, logits=logits, name="crossent")

        loss = tf.reduce_sum(crossent * tf.to_float(decoder_lengths)) / (batch_size * max_target_length)

        # Optimizer
        # TODO: Tutorial suggest to swap to SGD for alter iterations
        # optimizer = tf.train.AdamOptimizer()
        optimizer = tf.train.RMSPropOptimizer(learning_rate=0.001)
        gradients, v = zip(*optimizer.compute_gradients(loss))
        gradients, _ = tf.clip_by_global_norm(gradients, 10.0)
        optimize = optimizer.apply_gradients(zip(gradients, v))

        return encoder_char_ids_ph, encoder_lengths_ph, decoder_char_ids_ph, decoder_lengths, optimize, train_prediction, outputs

    if "EVAL" == mode:
        helperE = tf.contrib.seq2seq.GreedyEmbeddingHelper(
            decoder_embedding_weights,
            tf.fill([batch_size_ph], decoder_nchars-2), decoder_nchars-1)
        decoderE = tf.contrib.seq2seq.BasicDecoder(
            decoder_cell, helperE, decoder_initial_state,
            output_layer=projection_layer)
        outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoderE, maximum_iterations=20)

        translations = tf.identity(outputs.sample_id, name="decode")

        return encoder_char_ids_ph, encoder_lengths_ph, translations

def encode_chars(names):
    char_set = set()
    for name in names:
        char_set = char_set.union(name)
    return {k: v for v, k in enumerate(char_set)}

# TODO: Deduplicate this, same as in namefinder.py
def write_mapping(tags, output_filename):
    with open(output_filename, 'w', encoding='utf-8') as f:
        for i, tag in enumerate(tags):
            f.write('{}\n'.format(tag))

def main():

    if len(sys.argv) != 4:
        print("Usage normalizer.py train_file dev_file test_file")
        return

    checkpoints_path = "/tmp/model/checkpoints"

    source_train, target_train = load_data(sys.argv[1])
    source_dev, target_dev = load_data(sys.argv[2])
    source_test, target_test = load_data(sys.argv[3])

    source_char_dict = encode_chars(source_train + source_dev + source_test)
    source_char_dict[chr(0)] = 0

    target_char_dict = encode_chars(target_train + target_dev + target_test)

    # char id 2 is STX (Start of Text), and 3 ETX (End of Text)
    target_char_dict[chr(2)] = len(target_char_dict)
    target_char_dict[chr(3)] = len(target_char_dict)

    target_dict_rev = {v: k for k, v in target_char_dict.items()}

    batch_size = 20

    target_max_len = -1
    for token in (target_train + target_dev + target_test):
        target_max_len = max(target_max_len, len(token))

    # Increase size by one for termination char
    target_max_len += 1

    train_graph = tf.Graph()
    eval_graph = tf.Graph()

    with train_graph.as_default():
        t_encoder_char_ids_ph, t_encoder_lengths_ph, t_decoder_char_ids_ph, t_decoder_lengths, t_adam_optimize, t_train_prediction, t_dec_out = \
            create_graph("TRAIN", batch_size, len(source_char_dict), target_max_len, len(target_char_dict))
        train_saver = tf.train.Saver()
        train_sess = tf.Session()
        train_sess.run(tf.global_variables_initializer())

    with eval_graph.as_default():
        e_encoder_char_ids_ph, e_encoder_lengths_ph, e_dec_out = \
            create_graph("EVAL", batch_size, len(source_char_dict), target_max_len, len(target_char_dict))
        eval_saver = tf.train.Saver()

        eval_sess = tf.Session(graph=eval_graph)

    for epoch in range(20):
        print("Epoch " + str(epoch))

        with train_graph.as_default():
            for batch_index in range(floor(len(source_train) / batch_size)):
                if batch_index > 0 and batch_index % 100 == 0:
                    print("batch_index " + str(batch_index))

                target_batch, target_length, source_batch, source_length = \
                    mini_batch(target_char_dict, target_train, source_char_dict, source_train, batch_size, batch_index)

                # TODO: Add char dropout here ...
                for i, j in np.ndindex(source_batch.shape):
                    if random.uniform(0, 1) <= 0.0005:
                        source_batch[i][j] = 0

                feed_dict = {t_encoder_lengths_ph: source_length, t_encoder_char_ids_ph: source_batch,
                             t_decoder_lengths: target_length, t_decoder_char_ids_ph: target_batch}

                t1, dec1 = train_sess.run([t_adam_optimize, t_dec_out], feed_dict)
                dec2 = train_sess.run([t_dec_out], feed_dict)
                tv=1

            # Save train model, and restore it into the eval session
            checkpoint_path = train_saver.save(train_sess, checkpoints_path, global_step=epoch)
            eval_saver.restore(eval_sess, checkpoint_path)

        with eval_graph.as_default():
            count_correct = 0
            for batch_index in range(floor(len(source_dev) / batch_size)):
                target_batch, target_length, source_batch, source_length = \
                    mini_batch(target_char_dict, target_dev, source_char_dict, source_dev, batch_size, batch_index)

                begin = batch_index
                end = min(batch_index + batch_size, len(source_dev))
                target_strings = target_dev[begin:end]

                feed_dict = {e_encoder_lengths_ph: source_length, e_encoder_char_ids_ph: source_batch}
                result = eval_sess.run(e_dec_out, feed_dict)

                decoded_dates = []

                for coded_date in result:
                    date = ""
                    for char_id in coded_date:
                        if not char_id == len(target_char_dict) - 1:
                            date = date + (target_dict_rev[char_id])
                    decoded_dates.append(date)

                for i in range(len(target_strings)):
                    if target_strings[i] == decoded_dates[i]:
                        count_correct = count_correct + 1

            print("Dev: " + str(count_correct / len(target_dev)))

    with TemporaryDirectory() as temp_dir:

        temp_model_dir = temp_dir + "/model"


        with eval_graph.as_default():
            builder = tf.saved_model.builder.SavedModelBuilder(temp_model_dir)
            builder.add_meta_graph_and_variables(eval_sess, [tf.saved_model.tag_constants.SERVING])
            builder.save()

        write_mapping(source_char_dict, temp_model_dir + '/source_char_dict.txt')
        write_mapping(target_char_dict, temp_model_dir + '/target_char_dict.txt')

        zipf = zipfile.ZipFile("normalizer.zip", 'w', zipfile.ZIP_DEFLATED)

        for root, dirs, files in os.walk(temp_model_dir):
            for file in files:
                modelFile = os.path.join(root, file)
                zipf.write(modelFile, arcname=os.path.relpath(modelFile, temp_model_dir))

if __name__ == "__main__":
    main()
