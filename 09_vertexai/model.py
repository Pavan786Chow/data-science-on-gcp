# Copyright 2017-2021 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import logging
import os, json, math, shutil, time
import numpy as np
import tensorflow as tf

BUCKET = None
TF_VERSION = '2-' + tf.__version__[2:3]  # needed to choose container

DEVELOP_MODE = True
NUM_EXAMPLES = 5000 * 1000  # doesn't need to be precise but get order of magnitude right.

NBUCKETS = 5
NEMBEDS = 3
TRAIN_BATCH_SIZE = 64
DNN_HIDDEN_UNITS = '64,32'


def features_and_labels(features):
    label = features.pop('ontime')  # this is what we will train for
    return features, label


def read_dataset(pattern, batch_size, mode=tf.estimator.ModeKeys.TRAIN, truncate=None):
    dataset = tf.data.experimental.make_csv_dataset(pattern, batch_size, num_epochs=1)
    dataset = dataset.map(features_and_labels)
    if mode == tf.estimator.ModeKeys.TRAIN:
        dataset = dataset.shuffle(batch_size * 10)
        dataset = dataset.repeat()
    dataset = dataset.prefetch(1)
    if truncate is not None:
        dataset = dataset.take(truncate)
    return dataset


def create_model():
    real = {
        colname: tf.feature_column.numeric_column(colname)
        for colname in
        (
                'dep_delay,taxi_out,distance,dep_hour,is_weekday,' +
                'dep_airport_lat,dep_airport_lon,' +
                'arr_airport_lat,arr_airport_lon'
        ).split(',')
    }
    sparse = {
        'carrier': tf.feature_column.categorical_column_with_vocabulary_list('carrier',
                                                                             vocabulary_list='AS,VX,F9,UA,US,WN,HA,EV,MQ,DL,OO,B6,NK,AA'.split(
                                                                                 ',')),
        'origin': tf.feature_column.categorical_column_with_hash_bucket('origin', hash_bucket_size=1000),
        'dest': tf.feature_column.categorical_column_with_hash_bucket('dest', hash_bucket_size=1000),
    }

    inputs = {
        colname: tf.keras.layers.Input(name=colname, shape=(), dtype='float32')
        for colname in real.keys()
    }
    inputs.update({
        colname: tf.keras.layers.Input(name=colname, shape=(), dtype='string')
        for colname in sparse.keys()
    })

    latbuckets = np.linspace(20.0, 50.0, NBUCKETS).tolist()  # USA
    lonbuckets = np.linspace(-120.0, -70.0, NBUCKETS).tolist()  # USA
    disc = {}
    disc.update({
        'd_{}'.format(key): tf.feature_column.bucketized_column(real[key], latbuckets)
        for key in ['dep_airport_lat', 'arr_airport_lat']
    })
    disc.update({
        'd_{}'.format(key): tf.feature_column.bucketized_column(real[key], lonbuckets)
        for key in ['dep_airport_lon', 'arr_airport_lon']
    })

    # cross columns that make sense in combination
    sparse['dep_loc'] = tf.feature_column.crossed_column(
        [disc['d_dep_airport_lat'], disc['d_dep_airport_lon']], NBUCKETS * NBUCKETS)
    sparse['arr_loc'] = tf.feature_column.crossed_column(
        [disc['d_arr_airport_lat'], disc['d_arr_airport_lon']], NBUCKETS * NBUCKETS)
    sparse['dep_arr'] = tf.feature_column.crossed_column([sparse['dep_loc'], sparse['arr_loc']], NBUCKETS ** 4)

    # embed all the sparse columns
    embed = {
        'embed_{}'.format(colname): tf.feature_column.embedding_column(col, NEMBEDS)
        for colname, col in sparse.items()
    }
    real.update(embed)

    # one-hot encode the sparse columns
    sparse = {
        colname: tf.feature_column.indicator_column(col)
        for colname, col in sparse.items()
    }

    model = wide_and_deep_classifier(
        inputs,
        linear_feature_columns=sparse.values(),
        dnn_feature_columns=real.values(),
        dnn_hidden_units=DNN_HIDDEN_UNITS)

    return model


def wide_and_deep_classifier(inputs, linear_feature_columns, dnn_feature_columns, dnn_hidden_units):
    deep = tf.keras.layers.DenseFeatures(dnn_feature_columns, name='deep_inputs')(inputs)
    layers = [int(x) for x in dnn_hidden_units.split(',')]
    for layerno, numnodes in enumerate(layers):
        deep = tf.keras.layers.Dense(numnodes, activation='relu', name='dnn_{}'.format(layerno + 1))(deep)
    wide = tf.keras.layers.DenseFeatures(linear_feature_columns, name='wide_inputs')(inputs)
    both = tf.keras.layers.concatenate([deep, wide], name='both')
    output = tf.keras.layers.Dense(1, activation='sigmoid', name='pred')(both)
    model = tf.keras.Model(inputs, output)
    model.compile(optimizer='adam',
                  loss='binary_crossentropy',
                  metrics=['accuracy', rmse, tf.keras.metrics.AUC()])
    return model


def rmse(y_true, y_pred):
    return tf.sqrt(tf.reduce_mean(tf.square(y_pred - y_true)))


def train_and_evaluate(train_data_pattern, eval_data_pattern, test_data_pattern, export_dir, output_dir):
    train_batch_size = TRAIN_BATCH_SIZE
    if DEVELOP_MODE:
        eval_batch_size = 100
        steps_per_epoch = 3
        epochs = 2
        num_eval_examples = eval_batch_size * 10
    else:
        eval_batch_size = 100
        steps_per_epoch = NUM_EXAMPLES // train_batch_size
        epochs = 10
        num_eval_examples = eval_batch_size * 100

    train_dataset = read_dataset(train_data_pattern, train_batch_size)
    eval_dataset = read_dataset(eval_data_pattern, eval_batch_size, tf.estimator.ModeKeys.EVAL, num_eval_examples)

    checkpoint_path = '{}/checkpoints/flights.cpt'.format(output_dir)
    logging.info("Checkpointing to {}".format(checkpoint_path))
    cp_callback = tf.keras.callbacks.ModelCheckpoint(checkpoint_path,
                                                     save_weights_only=True,
                                                     verbose=1)

    model = create_model()
    history = model.fit(train_dataset,
                        validation_data=eval_dataset,
                        epochs=epochs,
                        steps_per_epoch=steps_per_epoch,
                        callbacks=[cp_callback])

    # export
    logging.info('Exporting to {}'.format(export_dir))
    tf.saved_model.save(model, export_dir)

    # write out final metric
    final_rmse = history.history['val_rmse'][-1]
    logging.info("Validation RMSE on {} samples = {}".format(num_eval_examples, final_rmse))

    if not DEVELOP_MODE and test_data_pattern is not None:
        logging.info("Evaluating over full test dataset")
        test_dataset = read_dataset(test_data_pattern, eval_batch_size, tf.estimator.ModeKeys.EVAL, None)
        final_metrics = model.evaluate(test_dataset)
        logging.info("Final metrics on full test dataset = {}".format(final_metrics))

    # hpt = hypertune.HyperTune()
    # hpt.report_hyperparameter_tuning_metric(hyperparameter_metric_tag='rmse', metric_value=final_rmse, global_step=1)


if __name__ == '__main__':
    logging.info("Tensorflow version " + tf.__version__)
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--bucket',
        help='Data will be read from gs://BUCKET/ch9/data and output will be in gs://BUCKET/ch9/trained_model',
        required=True
    )

    parser.add_argument(
        '--num_examples',
        help='Number of examples per epoch. Get order of magnitude correct.',
        default=5000000
    )

    # for hyper-parameter tuning
    parser.add_argument(
        '--train_batch_size',
        help='Number of examples to compute gradient on',
        type=int,
        default=64
    )
    parser.add_argument(
        '--nbuckets',
        help='Number of bins into which to discretize lats and lons',
        type=int,
        default=5
    )
    parser.add_argument(
        '--nembeds',
        help='Embedding dimension for categorical variables',
        type=int,
        default=3
    )
    parser.add_argument(
        '--dnn_hidden_units',
        help='Architecture of DNN part of wide-and-deep network',
        default='64,32'
    )
    parser.add_argument(
        '--develop',
        help='Train on a small subset in development',
        dest='develop',
        action='store_true')
    parser.set_defaults(develop=False)


    # parse args
    args = parser.parse_args()
    arguments = args.__dict__
    logging.getLogger().setLevel(logging.INFO)

    # set appropriate output directory
    BUCKET = arguments['bucket']

    # Change the output model directory etc. only if not already set by Vertex AI
    OUTPUT_DIR = 'gs://{}/ch9/trained_model'.format(BUCKET)
    OUTPUT_MODEL_DIR = os.getenv("AIP_MODEL_DIR")  # or None
    TRAIN_DATA_PATTERN = os.getenv("AIP_TRAINING_DATA_URI")
    EVAL_DATA_PATTERN = os.getenv("AIP_VALIDATION_DATA_URI")
    TEST_DATA_PATTERN = os.getenv("AIP_TEST_DATA_URI")
    if not OUTPUT_MODEL_DIR:
        OUTPUT_MODEL_DIR = os.path.join(OUTPUT_DIR,
                                        'export/flights_{}'.format(time.strftime("%Y%m%d-%H%M%S")))
    if not TRAIN_DATA_PATTERN:
        TRAIN_DATA_PATTERN = 'gs://{}/ch9/data/train*'.format(BUCKET)
    if not EVAL_DATA_PATTERN:
        EVAL_DATA_PATTERN = 'gs://{}/ch9/data/eval*'.format(BUCKET)

    logging.info("Reading training data from {}".format(TRAIN_DATA_PATTERN))
    logging.info('Writing trained model to {}'.format(OUTPUT_MODEL_DIR))

    # other global parameters
    NBUCKETS = arguments['nbuckets']
    NEMBEDS = arguments['nembeds']
    TRAIN_BATCH_SIZE = arguments['train_batch_size']
    DNN_HIDDEN_UNITS = arguments['dnn_hidden_units']
    DEVELOP_MODE = arguments['develop']

    # run
    train_and_evaluate(TRAIN_DATA_PATTERN, EVAL_DATA_PATTERN, TEST_DATA_PATTERN, OUTPUT_MODEL_DIR, OUTPUT_DIR)