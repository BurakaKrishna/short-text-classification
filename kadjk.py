import copy
import random
import sys
import numpy
from keras.models import Sequential
from keras import regularizers
from keras.layers import Dense, Dropout
from keras.layers import GlobalMaxPooling1D
from keras.layers import Embedding, LSTM, Bidirectional, TimeDistributed
from keras.callbacks import EarlyStopping, LearningRateScheduler
from keras_contrib.layers import CRF
from keras_contrib.utils import save_load_utils
from keras_contrib.metrics import crf_accuracy
from keras_contrib.losses import crf_loss
from keras.utils import to_categorical
from train_set_preferences import valid_set_idx, test_set_idx
from translate import read_translated_swda_corpus_data
from helpers import arrange_word_to_vec_dict, form_word_to_index_dict_from_dataset
from helpers import find_max_utterance_length, find_longest_conversation_length
from helpers import write_word_translation_list_to_file, write_word_list_to_file
from helpers import pad_dataset_to_equal_length

from fastText_multilingual.fasttext import FastVector

def form_datasets(talks, talk_names):
    print('Forming dataset appropriately...')
    
    x_train_list = []
    y_train_list = []
    x_valid_list = []
    y_valid_list = []
    x_test_list = []
    y_test_list = []
    t_i = 0
    for i in range(len(talks)):
        t = talks[i]
        if talk_names[i] in test_set_idx:
            x_test_list.append( t[0] )
            y_test_list.append( t[1] )
        if talk_names[i] in valid_set_idx:
            x_valid_list.append( t[0] )
            y_valid_list.append( t[1] )
        else:
            x_train_list.append( t[0] )
            y_train_list.append( t[1] )
        t_i += 1

    print('Formed dataset appropriately.')
    return ((x_train_list, y_train_list), (x_valid_list, y_valid_list), (x_test_list, y_test_list))

def form_mini_batches(dataset_x, max_mini_batch_size):
    num_conversations = len(dataset_x)

    # Form mini batches of equal-length conversations
    mini_batches = {}
    for i in range(num_conversations):
        num_utterances = len(dataset_x[i])
        if num_utterances in mini_batches:
            mini_batches[num_utterances].append( i )
        else:
            mini_batches[num_utterances] = [ i ]

    # Enforce max_batch_size on previously formed mini batches
    mini_batch_list = []
    for conversations in mini_batches.values():
        mini_batch_list += [conversations[x: x + max_mini_batch_size] for x in range(0, len(conversations), max_mini_batch_size)]

    return mini_batch_list


def kadjk_batch_generator(dataset_x, dataset_y, tag_indices,
                          mini_batch_list, max_conversation_length,
                          timesteps, num_word_dimensions, num_tags,
                          word_index_to_append, tag_index_to_append):
    num_mini_batches = len(mini_batch_list)

    # Shuffle the order of batches
    index_list = [x for x in range(num_mini_batches)]
    random.shuffle(index_list)

    k = -1
    while True:
        k = (k + 1) % len(index_list)
        index = index_list[k]
        conversation_indices = mini_batch_list[index]

        num_conversations = len(conversation_indices)
        batch_features = numpy.empty(shape = (num_conversations, max_conversation_length, timesteps),
                                     dtype = int)
        label_list = []

        for i in range(num_conversations):
            utterances = dataset_x[conversation_indices[i]]
            labels = copy.deepcopy(dataset_y[conversation_indices[i]])
            num_utterances = len(utterances)
            num_labels_to_append = max(0, max_conversation_length - len(labels))
            labels += [tag_index_to_append] * num_labels_to_append
            tags = to_categorical(labels, num_tags)
            del labels

            for j in range(num_utterances):
                utterance = copy.deepcopy(utterances[j])
                num_to_append = max(0, timesteps - len(utterance))
                if num_to_append > 0:
                    appendage = [word_index_to_append] * num_to_append
                    utterance += appendage

                batch_features[i][j] = utterance
                del utterance

            remaining_space = (max_conversation_length - num_utterances, timesteps)
            batch_features[i][num_utterances:] = numpy.ones(remaining_space) * word_index_to_append
            label_list.append(tags)

        batch_labels = numpy.array(label_list)
        del label_list

        yield batch_features, batch_labels



def prepare_kadjk_model(max_mini_batch_size,
                        max_conversation_length, timesteps, num_word_dimensions,
                        word_to_index, word_vec_dict,
                        num_tags, loss_function, optimizer):
    #Hyperparameters
    m = timesteps
    h = timesteps

    model = Sequential()

    dictionary_size = len(word_to_index) + 1
    print('dictionary_size:' + str(dictionary_size))

    embedding_weights = numpy.zeros((dictionary_size, num_word_dimensions))
    for word, index in word_to_index.items():
        embedding_weights[index, :] = word_vec_dict[word]

    # define inputs here
    embedding_layer = Embedding(dictionary_size, num_word_dimensions,
                                weights=[embedding_weights],
                                embeddings_regularizer=regularizers.l2(0.0001))
    model.add(TimeDistributed(embedding_layer,
                              input_shape=(max_conversation_length, timesteps)))

    model.add(TimeDistributed(Bidirectional(LSTM(m // 2, return_sequences=True,
                                            kernel_regularizer=regularizers.l2(0.0001)))))
    model.add(TimeDistributed(Dropout(0.2)))
    model.add(TimeDistributed(GlobalMaxPooling1D()))
    model.add(Bidirectional(LSTM(h // 2, return_sequences = True,
                                 kernel_regularizer=regularizers.l2(0.0001)), merge_mode='concat'))
    model.add(Dropout(0.2))
    crf = CRF(num_tags, sparse_target=False, kernel_regularizer=regularizers.l2(0.0001))
    print("Before CRF: %s" % str(model.output_shape))
    model.add(crf)
    model.compile(optimizer, loss = crf_loss,
                  metrics=[crf_accuracy])
    #TODO: Can we support providing custom loss functions like Lee-Dernoncourt model?
    return model

def learning_rate_scheduler(epoch, lr):
    if epoch % 5 == 0:
        if epoch > 0:
            return lr * 0.5
        else:
            return 1.0
    return lr

def train_kadjk(model, training, validation, num_epochs_to_train, tag_indices, max_mini_batch_size,
                max_conversation_length, timesteps, num_word_dimensions, num_tags,
                end_of_line_word_index, uninterpretable_label_index):
    training_mini_batch_list = form_mini_batches(training[0], max_mini_batch_size)
    validation_mini_batch_list = form_mini_batches(validation[0], max_mini_batch_size)

    num_training_steps = len(training_mini_batch_list)
    num_validation_steps = len(validation_mini_batch_list)

    early_stop = EarlyStopping(patience = 5)
    change_learning_rate = LearningRateScheduler(learning_rate_scheduler)

    model.fit_generator(kadjk_batch_generator(training[0], training[1], tag_indices,
                                              training_mini_batch_list, max_conversation_length,
                                              timesteps, num_word_dimensions, num_tags,
                                              end_of_line_word_index, uninterpretable_label_index),
                        steps_per_epoch = num_training_steps,
                        epochs = num_epochs_to_train,
                        validation_data = kadjk_batch_generator(validation[0], validation[1],
                                                                tag_indices,
                                                                validation_mini_batch_list, 
                                                                max_conversation_length, timesteps,
                                                                num_word_dimensions, num_tags,
                                                                end_of_line_word_index,
                                                                uninterpretable_label_index),
                        validation_steps = num_validation_steps,
                        callbacks = [early_stop, change_learning_rate])
    return model

def evaluate_kadjk(model, testing, tag_indices, max_mini_batch_size, max_conversation_length,
                   timesteps, num_word_dimensions, num_tags,
                   end_of_line_word_index, uninterpretable_label_index):
    testing_mini_batch_list = form_mini_batches(testing[0], max_mini_batch_size)
    num_testing_steps = len(testing_mini_batch_list)
    score = model.evaluate_generator(kadjk_batch_generator(testing[0], testing[1],
                                                           tag_indices,
                                                           testing_mini_batch_list, 
                                                           max_conversation_length, timesteps,
                                                           num_word_dimensions, num_tags,
                                                           end_of_line_word_index,
                                                           uninterpretable_label_index),
                                     steps = num_testing_steps)
    print("len(score):" + str(len(score)))
    print("score:" + str(score))
    return score[1]

def kadjk(dataset_loading_function, dataset_file_path,
          embedding_loading_function, 
          source_lang, source_lang_embedding_file,
          target_lang, target_lang_embedding_file,
          translation_list_file,
          word_translation_list,
          translated_list_file,
          translated_word_list,
          target_test_data_path,
          num_epochs_to_train, loss_function, optimizer,
          shuffle_words, load_from_model_file, save_to_model_file):
    talks_read, talk_names, tag_indices, tag_occurances = dataset_loading_function(dataset_file_path)
    read_translated_swda_corpus_data(talks_read, talk_names, target_test_data_path, target_lang)

    max_utterance = 0
    num_utterances = 0
    if word_translation_list is None:
        word_vec_dict = {}
        for k, c in enumerate(talks_read):
            num_utterances += len(c[0])
            for u in c[0]:
                max_utterance = max(max_utterance, len(u))
                for i in range(len(u)):
                    w = u[i]
                    if w.rstrip(',') != w or w.rstrip('.') != w or w.rstrip('?') != w or w.rstrip('!') != w:
                        u[i] = w.rstrip(',').rstrip('.').rstrip('?').rstrip('!')
                    prefix = target_lang if (talk_names[k] in test_set_idx) else source_lang
                    word_vec_dict[prefix + u[i].lower()] = True
        if translation_list_file is not None:
            write_word_list_to_file(translation_list_file, list( word_vec_dict.keys() ))
    else:
        word_vec_dict = {word:True for word in word_translation_list}

    words_translated = []
    if translated_word_list is not None:
        words_translated = translated_word_list
        for translation_pair in words_translated:
            if len(translation_pair) != 2:
                print("Problem!")
                print("len:%d - str:%s" %(len(translation_pair), str(translation_pair)))
                return
            if translation_pair[0] not in word_vec_dict:
                print("WARNING! Word dictionary and translated word file seem to be irrelevant!")
            word_vec_dict[ translation_pair[0] ] = translation_pair[1]
    total_words = len(word_vec_dict)
    
    target_dictionary = FastVector(vector_file=target_lang_embedding_file)
    print("Target  monolingual language data loaded successfully.")

    if len(words_translated) < total_words:
        source_dictionary = FastVector(vector_file=source_lang_embedding_file)
        print("Source monolingual language data loaded successfully.")
        transformation_matrix_path='fastText_multilingual/alignment_matrices/%s.txt'
        source_transform_matrix_file = transformation_matrix_path % source_lang
        target_transform_matrix_file = transformation_matrix_path % target_lang
        source_dictionary.apply_transform(source_transform_matrix_file)
        print("Transformation data applied to source language.")
        target_dictionary.apply_transform(target_transform_matrix_file)
        print("Transformation data applied to target language.")
        print("Translating words seen in dataset:")

        try:
            words_seen = 0
            for w, val in word_vec_dict.items():
                if type(val) == bool and w[:len(source_lang)] == source_lang:
                    word = w[len(source_lang):]
                    try:
                        translation = target_dictionary.translate_inverted_softmax(source_dictionary[word],
                                                                                   source_dictionary, 1500,
                                                                                   recalculate=False)
        #                translation = target_dictionary.translate_nearest_neighbor(source_dictionary[word])
                        word_vec_dict[w] = translation
                        words_translated.append((w, translation))
                    except KeyError as e:
                        pass
                words_seen += 1
                if words_seen % 100 == 0:
                    print("\t- Translated %d out of %d." % (words_seen, total_words))
        except KeyboardInterrupt as e:
            if translated_list_file is not None:
                write_word_translation_list_to_file(translated_list_file, words_translated)
            sys.exit(0)
        print("Translation complete.")

        del source_dictionary
        del target_dictionary

        if translated_list_file is not None:
            write_word_translation_list_to_file(translated_list_file, words_translated)

        print("Source and target dictionaries are deleted.")

        target_dictionary = FastVector(vector_file=target_lang_embedding_file)

    for k, w in enumerate(word_vec_dict):
        if type( word_vec_dict[w] ) == str:
            if w[:len(source_lang)] == source_lang:
                word = w[len(source_lang):]
                try:
                    word_vec_dict[w] = target_dictionary[ word_vec_dict[w] ]
                except KeyError as e:
                    print("Key error occured!")
                    print('"%s" - "%s" - "%s"' % (w, word, word_vec_dict[w]))
                    pass
                num_word_dimensions = len(word_vec_dict[w])

    del target_dictionary

    print("Vector counterparts are placed.")

    arrange_word_to_vec_dict(talks_read, talk_names, source_lang, target_lang, word_vec_dict, num_word_dimensions)
    word_to_index = form_word_to_index_dict_from_dataset(word_vec_dict)

    print("Dataset arranged.")

    end_of_line_word = '<unk>'
    end_of_line_word_index = len(word_to_index) + 1
    word_to_index[end_of_line_word] = end_of_line_word_index
    word_vec_dict[end_of_line_word] = numpy.random.random(num_word_dimensions)

    uninterpretable_label_index = tag_indices['%']

    talks = [([[word_to_index[(target_lang if (talk_names[k] in test_set_idx) else source_lang) + w.lower()] for w in u] for u in c[0]], c[1]) for k, c in enumerate(talks_read)]
    talks_read.clear()

    timesteps = find_max_utterance_length(talks)
    max_conversation_length = find_longest_conversation_length(talks)
    num_tags = len(tag_indices.keys())

    training, validation, testing = form_datasets(talks, talk_names)
    talk_names.clear()
    talks.clear()

    print("Training, validation and tesing datasets are formed.")

    if shuffle_words:
        for talk in training[0]:
            for utterance in talk:
                random.shuffle(utterance)

    pad_dataset_to_equal_length(training)
    pad_dataset_to_equal_length(validation)
    pad_dataset_to_equal_length(testing)

    for i in range(len(training)):
        if len(training[0][i]) != len(training[1][i]):
            print("Found it: " + str(i))
            return

    print("Preparing the learning model...")

    max_mini_batch_size = 64
    model = prepare_kadjk_model(max_mini_batch_size, max_conversation_length,
                                timesteps, num_word_dimensions, word_to_index, word_vec_dict,
                                num_tags, loss_function, optimizer)
    print('word_vec_dict:' + str(len(word_vec_dict)))
    print('word_to_index:' + str(len(word_to_index)))

    print("Checking indices of word_to_index:")
    index_to_word = {val:key for key, val in word_to_index.items()}
    for i in range(0, len(word_to_index)):
        if i not in index_to_word:
            print(str(i))
    print("Checked indices of word_to_index.")

    word_vec_dict.clear()
    word_to_index.clear()

    if load_from_model_file is not None:
        print("Doing a dummy training before loading the provided weights:")
        train_kadjk(model, ([training[0][0]], [training[1][0]]),
                    ([validation[0][0]], [validation[1][0]]), 1, tag_indices,
                    max_mini_batch_size, max_conversation_length,
                    timesteps, num_word_dimensions, num_tags,
                    end_of_line_word_index, uninterpretable_label_index)
        print("Finished the dummy training. Now loading weights.")
        save_load_utils.load_all_weights(model, load_from_model_file)
        print("Loaded the weights.")

    print("BEGINNING THE TRAINING...")
    if num_epochs_to_train > 0:
        train_kadjk(model, training, validation, num_epochs_to_train, tag_indices,
                    max_mini_batch_size, max_conversation_length,
                    timesteps, num_word_dimensions, num_tags,
                    end_of_line_word_index, uninterpretable_label_index)

#    score = evaluate_kadjk(model, testing, tag_indices, max_mini_batch_size,
#                           max_conversation_length, timesteps,
#                           num_word_dimensions, num_tags,
#                           end_of_line_word_index, uninterpretable_label_index)

    print("Accuracy: " + str(score * 100) + "%")

    if save_to_model_file:
        save_load_utils.save_all_weights(model, save_to_model_file)
    return model

