# -*- coding: utf-8 -*-
"""
Use Swabian Instruments PulseStreamer8/2 as a pulse generator.

Protobuf (pb2) and grpc files generated from pulse_streamer.proto
file available at https://www.swabianinstruments.com/static/documentation/PulseStreamer/sections/interface.html#grpc-interface.

Regenerate files for an update proto file using the following:
python3 -m grpc_tools.protoc -I=./ --python_out=. --grpc_python_out=. ./pulse_streamer.proto

Qudi is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Qudi is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Qudi. If not, see <http://www.gnu.org/licenses/>.

Copyright (c) the Qudi Developers. See the COPYRIGHT.txt file at the
top-level directory of this distribution and at <https://github.com/Ulm-IQO/qudi/>
"""

from core.module import Base, ConfigOption
from core.util.modules import get_home_dir
from interface.pulser_interface import PulserInterface, PulserConstraints
from collections import OrderedDict

import grpc
import os
import hardware.swabian_instruments.pulse_streamer_pb2 as pulse_streamer_pb2
import dill
import numpy as np #EB 20.09.18 Added for write_waveform method

class PulseStreamer(Base, PulserInterface):
    """Methods to control PulseStreamer.
    """
    _modclass = 'pulserinterface'
    _modtype = 'hardware'

    _pulsestreamer_ip = ConfigOption('pulsestreamer_ip', '192.168.1.100', missing='warn')
    _laser_channel = ConfigOption('laser_channel', 0, missing='warn')
    _uw_x_channel = ConfigOption('uw_x_channel', 3, missing='warn')

    _current_waveform_name = '' #class variable in which I store the waveform name (modified by E.B)
    _current_waveform = [] #class variable in which I store the waveform (modified by E.B)

    def __init__(self, config, **kwargs):
        super().__init__(config=config, **kwargs)
        self._currently_loaded_waveform = ''  # loaded and armed waveform name

        if 'pulsed_file_dir' in config.keys():
            self.pulsed_file_dir = config['pulsed_file_dir']

            if not os.path.exists(self.pulsed_file_dir):
                homedir = get_home_dir()
                self.pulsed_file_dir = os.path.join(homedir, 'pulsed_files')
                self.log.warning('The directory defined in parameter '
                            '"pulsed_file_dir" in the config for '
                            'PulseStreamer does not exist!\n'
                            'The default home directory\n{0}\n will be taken '
                            'instead.'.format(self.pulsed_file_dir))
        else:
            homedir = get_home_dir()
            self.pulsed_file_dir = os.path.join(homedir, 'pulsed_files')
            self.log.warning('No parameter "pulsed_file_dir" was specified in the config for '
                             'PulseStreamer as directory for the pulsed files!\nThe default home '
                             'directory\n{0}\nwill be taken instead.'.format(self.pulsed_file_dir))

        self.host_waveform_directory = self._get_dir_for_name('sampled_hardware_files')

        self.current_status = -1
        self.sample_rate = 1e9
        self.current_loaded_asset = None

        self._channel = grpc.insecure_channel(self._pulsestreamer_ip + ':50051')

    def on_activate(self):
        """ Establish connection to pulse streamer and tell it to cancel all operations """
        self.pulse_streamer = pulse_streamer_pb2.PulseStreamerStub(self._channel)
        self.pulser_off()
        self.current_status = 0

    def on_deactivate(self):
        del self.pulse_streamer

    def get_constraints(self):
        """ Retrieve the hardware constrains from the Pulsing device.

        @return dict: dict with constraints for the sequence generation and GUI

        Provides all the constraints (e.g. sample_rate, amplitude,
        total_length_bins, channel_config, ...) related to the pulse generator
        hardware to the caller.
        The keys of the returned dictionary are the str name for the constraints
        (which are set in this method). No other keys should be invented. If you
        are not sure about the meaning, look in other hardware files to get an
        impression. If still additional constraints are needed, then they have
        to be add to all files containing this interface.
        The items of the keys are again dictionaries which have the generic
        dictionary form:
            {'min': <value>,
             'max': <value>,
             'step': <value>,
             'unit': '<value>'}

        Only the keys 'activation_config' and differs, since it contain the
        channel configuration/activation information.

        If the constraints cannot be set in the pulsing hardware (because it
        might e.g. has no sequence mode) then write just zero to each generic
        dict. Note that there is a difference between float input (0.0) and
        integer input (0).
        ALL THE PRESENT KEYS OF THE CONSTRAINTS DICT MUST BE ASSIGNED!
        """
        constraints = PulserConstraints()

        # The file formats are hardware specific.
        constraints.waveform_format = ['pstream']
        constraints.sequence_format = []

        constraints.sample_rate.min = 1e9
        constraints.sample_rate.max = 1e9
        constraints.sample_rate.step = 0
        constraints.sample_rate.default = 1e9

        constraints.d_ch_low.min = 0.0
        constraints.d_ch_low.max = 0.0
        constraints.d_ch_low.step = 0.0
        constraints.d_ch_low.default = 0.0

        constraints.d_ch_high.min = 3.3
        constraints.d_ch_high.max = 3.3
        constraints.d_ch_high.step = 0.0
        constraints.d_ch_high.default = 3.3

        # sample file length max is not well-defined for PulseStreamer, which collates sequential identical pulses into
        # one. Total number of not-sequentially-identical pulses which can be stored: 1 M.
        constraints.waveform_length.min = 1
        constraints.waveform_length.max = 134217728
        constraints.waveform_length.step = 1
        constraints.waveform_length.default = 1

        # the name a_ch<num> and d_ch<num> are generic names, which describe UNAMBIGUOUSLY the
        # channels. Here all possible channel configurations are stated, where only the generic
        # names should be used. The names for the different configurations can be customary chosen.
        activation_config = OrderedDict()
        activation_config['all'] = {'d_ch1', 'd_ch2', 'd_ch3', 'd_ch4', 'd_ch5', 'd_ch6', 'd_ch7',
                                    'd_ch8'}
        constraints.activation_config = activation_config

        return constraints

    def pulser_on(self):
        """ Switches the pulsing device on.

        @return int: error code (0:OK, -1:error)
        """
        # start the pulse sequence
        self.pulse_streamer.stream(self._sequence)
        self.log.info('Asset uploaded to PulseStreamer')
        self.pulse_streamer.startNow(pulse_streamer_pb2.VoidMessage())
        self.current_status = 1
        return 0

    def pulser_off(self):
        """ Switches the pulsing device off.

        @return int: error code (0:OK, -1:error)
        """
        # stop the pulse sequence
        channels = self._convert_to_bitmask([self._laser_channel, self._uw_x_channel])
        self.pulse_streamer.constant(pulse_streamer_pb2.PulseMessage(ticks=0, digi=channels, ao0=0, ao1=0))
        self.current_status = 0
        return 0

    def load_waveform(self, load_dict): ## Modified by E.B
        """ Loads a waveform to the specified channel of the pulsing device.

        @param dict|list load_dict: a dictionary with keys being one of the
                                    available channel index and values being the
                                    name of the already written waveform to load
                                    into the channel. Examples:

                                        {1: rabi_ch1, 2: rabi_ch2}
                                    or
                                        {1: rabi_ch2, 2: rabi_ch1}

                                    If just a list of waveform names if given,
                                    the channel association will be invoked from
                                    the channel suffix '_ch1', '_ch2' etc. A
                                    possible configuration can be e.g.

                                        ['rabi_ch1', 'rabi_ch2', 'rabi_ch3']

        @return dict: Dictionary containing the actually loaded waveforms per
                      channel.

        For devices that have a workspace (i.e. AWG) this will load the waveform
        from the device workspace into the channel. For a device without mass
        memory, this will make the waveform/pattern that has been previously
        written with self.write_waveform ready to play.

        Please note that the channel index used here is not to be confused with the number suffix
        in the generic channel descriptors (i.e. 'd_ch1', 'a_ch1'). The channel index used here is
        highly hardware specific and corresponds to a collection of digital and analog channels
        being associated to a SINGLE wavfeorm asset.
        """
        # Since only one waveform can be present at a time check if only a single name is given
        if isinstance(load_dict, list):
            waveforms = list(set(load_dict))
        elif isinstance(load_dict, dict):
            waveforms = list(set(load_dict.values()))
        else:
            self.log.error('Method load_waveform expects a list of waveform names or a dict.')
            return self.get_loaded_assets()

        if len(waveforms) != 1:
            self.log.error('PULSE STREAMER pulser expects exactly one waveform name for load_waveform.')
            return self.get_loaded_assets()

        waveform = waveforms[0]
        if waveform != self._current_waveform_name:
            self.log.error('No waveform by the name "{0}" generated for PULSE STREAMER pulser.\n'
                           'Only one waveform at a time can be held.'.format(waveform))
            return self.get_loaded_assets()

        pulse_sequence = []
        for pulse in self._current_waveform:
            pulse_sequence.append(pulse_streamer_pb2.PulseMessage(ticks=pulse[0], digi=pulse[1], ao0=0, ao1=1))

        blank_pulse = pulse_streamer_pb2.PulseMessage(ticks=0, digi=0, ao0=0, ao1=0)
        laser_on = pulse_streamer_pb2.PulseMessage(ticks=0, digi=self._convert_to_bitmask([self._laser_channel]), ao0=0,
                                                   ao1=0)
        laser_and_uw_channels = self._convert_to_bitmask([self._laser_channel, self._uw_x_channel])
        laser_and_uw_on = pulse_streamer_pb2.PulseMessage(ticks=0, digi=laser_and_uw_channels, ao0=0, ao1=0)
        self. \
            _sequence = pulse_streamer_pb2.SequenceMessage(pulse=pulse_sequence, n_runs=0, initial=laser_on,
                                                           final=laser_and_uw_on, underflow=blank_pulse, start=1)

        self._currently_loaded_waveform = waveform
        return self.get_loaded_assets()

    def load_sequence(self, sequence_name):
        """ Loads a sequence to the channels of the device in order to be ready for playback.
        For devices that have a workspace (i.e. AWG) this will load the sequence from the device
        workspace into the channels.
        For a device without mass memory this will make the waveform/pattern that has been
        previously written with self.write_waveform ready to play.

        @param sequence_name:  dict|list, a dictionary with keys being one of the available channel
                                      index and values being the name of the already written
                                      waveform to load into the channel.
                                      Examples:   {1: rabi_ch1, 2: rabi_ch2} or
                                                  {1: rabi_ch2, 2: rabi_ch1}
                                      If just a list of waveform names if given, the channel
                                      association will be invoked from the channel
                                      suffix '_ch1', '_ch2' etc.

        @return dict: Dictionary containing the actually loaded waveforms per channel.
        """
        self.log.warning('Pulse Streamer 8/2 has no sequencing capabilities.\n'
                         'load_sequence call ignored.')
        return

    def get_loaded_assets(self):
        """
        Retrieve the currently loaded asset names for each active channel of the device.
        The returned dictionary will have the channel numbers as keys.
        In case of loaded waveforms the dictionary values will be the waveform names.
        In case of a loaded sequence the values will be the sequence name appended by a suffix
        representing the track loaded to the respective channel (i.e. '<sequence_name>_1').

        @return (dict, str): Dictionary with keys being the channel number and values being the
                             respective asset loaded into the channel,
                             string describing the asset type ('waveform' or 'sequence')
        """
        asset_type = 'waveform' if self._currently_loaded_waveform else None
        asset_dict = {chnl_num: self._currently_loaded_waveform for chnl_num in range(1, 8)} #channel num start from 1
        return asset_dict, asset_type

    def clear_all(self): ## modified by E.B
        """ Clears all loaded waveforms from the pulse generators RAM.

        @return int: error code (0:OK, -1:error)

        Unused for digital pulse generators without storage capability
        (PulseBlaster, FPGA).
        """
        self._currently_loaded_waveform = ''
        self._current_waveform_name = ''
        self._current_waveform = []
        print(self._current_waveform)  # Added by EB for debugging

        return 0

    def get_status(self):
        """ Retrieves the status of the pulsing hardware

        @return (int, dict): tuple with an interger value of the current status
                             and a corresponding dictionary containing status
                             description for all the possible status variables
                             of the pulse generator hardware.
        """
        status_dic = dict()
        status_dic[-1] = 'Failed Request or Failed Communication with device.'
        status_dic[0] = 'Device has stopped, but can receive commands.'
        status_dic[1] = 'Device is active and running.'

        return self.current_status, status_dic

    def get_sample_rate(self):
        """ Get the sample rate of the pulse generator hardware

        @return float: The current sample rate of the device (in Hz)

        Do not return a saved sample rate in a class variable, but instead
        retrieve the current sample rate directly from the device.
        """
        return self.sample_rate

    def set_sample_rate(self, sample_rate):
        """ Set the sample rate of the pulse generator hardware.

        @param float sample_rate: The sampling rate to be set (in Hz)

        @return float: the sample rate returned from the device.

        Note: After setting the sampling rate of the device, retrieve it again
              for obtaining the actual set value and use that information for
              further processing.
        """
        self.log.debug('PulseStreamer sample rate cannot be configured')
        return self.sample_rate

    def get_analog_level(self, amplitude=None, offset=None):
        """ Retrieve the analog amplitude and offset of the provided channels.

        @param list amplitude: optional, if a specific amplitude value (in Volt
                               peak to peak, i.e. the full amplitude) of a
                               channel is desired.
        @param list offset: optional, if a specific high value (in Volt) of a
                            channel is desired.

        @return: (dict, dict): tuple of two dicts, with keys being the channel
                               number and items being the values for those
                               channels. Amplitude is always denoted in
                               Volt-peak-to-peak and Offset in (absolute)
                               Voltage.
        """
        return {}, {}

    def set_analog_level(self, amplitude=None, offset=None):
        """ Set amplitude and/or offset value of the provided analog channel.

        @param dict amplitude: dictionary, with key being the channel and items
                               being the amplitude values (in Volt peak to peak,
                               i.e. the full amplitude) for the desired channel.
        @param dict offset: dictionary, with key being the channel and items
                            being the offset values (in absolute volt) for the
                            desired channel.

        @return (dict, dict): tuple of two dicts with the actual set values for
                              amplitude and offset.

        If nothing is passed then the command will return two empty dicts.
        """
        return {}, {}

    def get_digital_level(self, low=None, high=None):
        """ Retrieve the digital low and high level of the provided channels.

        @param list low: optional, if a specific low value (in Volt) of a
                         channel is desired.
        @param list high: optional, if a specific high value (in Volt) of a
                          channel is desired.

        @return: (dict, dict): tuple of two dicts, with keys being the channel
                               number and items being the values for those
                               channels. Both low and high value of a channel is
                               denoted in (absolute) Voltage.

        Note: Do not return a saved low and/or high value but instead retrieve
              the current low and/or high value directly from the device.

        If no entries provided then the levels of all channels where simply
        returned. If no digital channels provided, return just an empty dict.

        Example of a possible input:
            low = [1,4]
        to obtain the low voltage values of digital channel 1 an 4. A possible
        answer might be
            {1: -0.5, 4: 2.0} {}
        since no high request was performed.

        The major difference to analog signals is that digital signals are
        either ON or OFF, whereas analog channels have a varying amplitude
        range. In contrast to analog output levels, digital output levels are
        defined by a voltage, which corresponds to the ON status and a voltage
        which corresponds to the OFF status (both denoted in (absolute) voltage)

        In general there is no bijective correspondence between
        (amplitude, offset) and (value high, value low)!
        """
        if low is None:
            low = []
        if high is None:
            high = []
        low_dict = {}
        high_dict = {}
        if low is [] and high is []:
            for channel in range(8):
                low_dict[channel] = 0.0
                high_dict[channel] = 3.3
        else:
            for channel in low:
                low_dict[channel] = 0.0
            for channel in high:
                high_dict[channel] = 3.3
        return low_dict, high_dict

    def set_digital_level(self, low=None, high=None):
        """ Set low and/or high value of the provided digital channel.

        @param dict low: dictionary, with key being the channel and items being
                         the low values (in volt) for the desired channel.
        @param dict high: dictionary, with key being the channel and items being
                         the high values (in volt) for the desired channel.

        @return (dict, dict): tuple of two dicts where first dict denotes the
                              current low value and the second dict the high
                              value.

        If nothing is passed then the command will return two empty dicts.

        Note: After setting the high and/or low values of the device, retrieve
              them again for obtaining the actual set value(s) and use that
              information for further processing.

        The major difference to analog signals is that digital signals are
        either ON or OFF, whereas analog channels have a varying amplitude
        range. In contrast to analog output levels, digital output levels are
        defined by a voltage, which corresponds to the ON status and a voltage
        which corresponds to the OFF status (both denoted in (absolute) voltage)

        In general there is no bijective correspondence between
        (amplitude, offset) and (value high, value low)!
        """
        if low is None:
            low = {}
        if high is None:
            high = {}
        self.log.warning('PulseStreamer logic level cannot be adjusted!')
        return 0

    def get_active_channels(self,  ch=None):
        """ Get the active channels of the pulse generator hardware.

              @param list ch: optional, if specific analog or digital channels are needed to be asked
                              without obtaining all the channels.

              @return dict:  where keys denoting the channel string and items boolean expressions whether
                             channel are active or not.

              Example for an possible input (order is not important):
                  ch = ['a_ch2', 'd_ch2', 'a_ch1', 'd_ch5', 'd_ch1']
              then the output might look like
                  {'a_ch2': True, 'd_ch2': False, 'a_ch1': False, 'd_ch5': True, 'd_ch1': False}

              If no parameter (or None) is passed to this method all channel states will be returned.
        """
        if ch is None:
            ch = {}
        d_ch_dict = {}
        if len(ch) < 1:
            for chnl in range(1, 9):
                d_ch_dict['d_ch{0}'.format(chnl)] = True
        else:
            for channel in ch:
                d_ch_dict[channel] = True
        return d_ch_dict

    def set_active_channels(self, ch=None):
        """
               Set the active/inactive channels for the pulse generator hardware.
               The state of ALL available analog and digital channels will be returned
               (True: active, False: inactive).
               The actually set and returned channel activation must be part of the available
               activation_configs in the constraints.
               You can also activate/deactivate subsets of available channels but the resulting
               activation_config must still be valid according to the constraints.
               If the resulting set of active channels can not be found in the available
               activation_configs, the channel states must remain unchanged.

               @param dict ch: dictionary with keys being the analog or digital string generic names for
                               the channels (i.e. 'd_ch1', 'a_ch2') with items being a boolean value.
                               True: Activate channel, False: Deactivate channel

               @return dict: with the actual set values for ALL active analog and digital channels

               If nothing is passed then the command will simply return the unchanged current state.

               Note: After setting the active channels of the device, use the returned dict for further
                     processing.

               Example for possible input:
                   ch={'a_ch2': True, 'd_ch1': False, 'd_ch3': True, 'd_ch4': True}
               to activate analog channel 2 digital channel 3 and 4 and to deactivate
               digital channel 1. All other available channels will remain unchanged.
               """
        if ch is None:
            ch = {}
        d_ch_dict = {
            'd_ch1': True,
            'd_ch2': True,
            'd_ch3': True,
            'd_ch4': True,
            'd_ch5': True,
            'd_ch6': True,
            'd_ch7': True,
            'd_ch8': True}
        return d_ch_dict

    def write_waveform(self, name, analog_samples, digital_samples, is_first_chunk, is_last_chunk,
                       total_number_of_samples): ## Modified by E.B

        """
        EB 20.09.2018: the whole ensemble is written as a whole in one big chunk
        Write a new waveform or append samples to an already existing waveform on the device memory.
        The flags is_first_chunk and is_last_chunk can be used as indicator if a new waveform should
        be created or if the write process to a waveform should be terminated.

        NOTE: All sample arrays in analog_samples and digital_samples must be of equal length!

        @param str name: the name of the waveform to be created/append to
        @param dict analog_samples: keys are the generic analog channel names (i.e. 'a_ch1') and
                                    values are 1D numpy arrays of type float32 containing the
                                    voltage samples.
        @param dict digital_samples: keys are the generic digital channel names (i.e. 'd_ch1') and
                                     values are 1D numpy arrays of type bool containing the marker
                                     states.
        @param bool is_first_chunk: Flag indicating if it is the first chunk to write.
                                    If True this method will create a new empty wavveform.
                                    If False the samples are appended to the existing waveform.
        @param bool is_last_chunk:  Flag indicating if it is the last chunk to write.
                                    Some devices may need to know when to close the appending wfm.
        @param int total_number_of_samples: The number of sample points for the entire waveform
                                            (not only the currently written chunk)

        @return (int, list): Number of samples written (-1 indicates failed process) and list of
                             created waveform names
        """


        # Initialize waveform array if this is the first chunk to write
        if is_first_chunk:
            self._current_waveform_name = name
            self._current_waveform=[]

        # Building the matrix of digital_samples_mat from the dictionary of digital_samples
        total_number_of_samples = len(digital_samples['d_ch1'])
        digital_samples_mat = np.zeros((8, total_number_of_samples))
        digital_samples_mat = np.array(
            [digital_samples['d_ch1'], digital_samples['d_ch2'], digital_samples['d_ch3'],
             digital_samples['d_ch4'], digital_samples['d_ch5'], digital_samples['d_ch6'],
             digital_samples['d_ch7'], digital_samples['d_ch8']])

        channel_number = digital_samples_mat.shape[0]
        digital_samples_mat = np.transpose(digital_samples_mat)

        # fetch locations where digital channel states change, and eliminate duplicates
        new_channel_indices = np.where(digital_samples_mat[:-1, :] != digital_samples_mat[1:, :])[0]
        new_channel_indices = np.unique(new_channel_indices)

        # add in indices for the start and end of the sequence to simplify iteration
        new_channel_indices = np.insert(new_channel_indices, 0, [-1])
        new_channel_indices = np.insert(new_channel_indices, new_channel_indices.size,
                                        [digital_samples_mat.shape[0] - 1])

        pulses = []
        for new_channel_index in range(1, new_channel_indices.size):
            pulse = [new_channel_indices[new_channel_index] - new_channel_indices[new_channel_index - 1],
                     self._convert_to_bitmask_bool(
                         digital_samples_mat[new_channel_indices[new_channel_index - 1] + 1, :])]
            pulses.append(pulse)

        #Write the waveform
        self._current_waveform=pulses
        print(self._current_waveform)# Added by EB for debugging

        return total_number_of_samples, [self._current_waveform_name]

    def write_sequence(self, name, sequence_parameters):
        """
        Write a new sequence on the device memory.

        @param name: str, the name of the waveform to be created/append to
        @param sequence_parameters: dict, dictionary containing the parameters for a sequence

        @return: int, number of sequence steps written (-1 indicates failed process)
        """
        self.log.warning('Pulse Streamer 8/2 has no sequencing capabilities.\n'
                         'write_sequence call ignored.')
        return -1

    def get_waveform_names(self): ## Modified by E.B
        """ Retrieve the names of all uploaded waveforms on the device.

        @return list: List of all uploaded waveform name strings in the device workspace.
        """
        return [self._current_waveform_name]

    def get_sequence_names(self):
        """ Retrieve the names of all uploaded sequence on the device.

        @return list: List of all uploaded sequence name strings in the device workspace.
        """
        return list()

    def delete_waveform(self, waveform_name):
        """ Delete the waveform with name "waveform_name" from the device memory.

        @param str waveform_name: The name of the waveform to be deleted
                                  Optionally a list of waveform names can be passed.

        @return list: a list of deleted waveform names.
        """
        return

    def delete_sequence(self, sequence_name):
        """ Delete the sequence with name "sequence_name" from the device memory.

        @param str sequence_name: The name of the sequence to be deleted
                                  Optionally a list of sequence names can be passed.

        @return list: a list of deleted sequence names.
        """
        return list()

    def get_interleave(self):
        """ Check whether Interleave is ON or OFF in AWG.

        @return bool: True: ON, False: OFF

        Unused for pulse generator hardware other than an AWG.
        """
        return False

    def set_interleave(self, state=False):
        """ Turns the interleave of an AWG on or off.

        @param bool state: The state the interleave should be set to
                           (True: ON, False: OFF)

        @return bool: actual interleave status (True: ON, False: OFF)

        Note: After setting the interleave of the device, retrieve the
              interleave again and use that information for further processing.

        Unused for pulse generator hardware other than an AWG.
        """
        return False

    def reset(self):
        """ Reset the device.

        @return int: error code (0:OK, -1:error)
        """
        channels = self._convert_to_bitmask([self._laser_channel, self._uw_x_channel])
        self.pulse_streamer.constant(pulse_streamer_pb2.PulseMessage(ticks=0, digi=channels, ao0=0, ao1=0))
        self.pulse_streamer.constant(laser_on)
        return 0

    def has_sequence_mode(self):
        """ Asks the pulse generator whether sequence mode exists.

        @return: bool, True for yes, False for no.
        """
        return False

    def _convert_to_bitmask_bool(self, active_channels): ## Modified by E.B
        """ Convert a boolean np_array of active channels into a bitmask.
        @param numpy.array active_channels: the list of active channels like
                            e.g. [0,4,7]. Note that the channels start from 0.
        @return int: The channel-list is converted into a bitmask (an sequence
                     of 1 and 0). The returned integer corresponds to such a
                     bitmask.
        Note that you can get a binary representation of an integer in python
        if you use the command bin(<integer-value>). All higher unneeded digits
        will be dropped, i.e. 0b00100 is turned into 0b100. Examples are
            bin(0) =    0b0
            bin(1) =    0b1
            bin(8) = 0b1000
        Each bit value (read from right to left) corresponds to the fact that a
        channel is on or off. I.e. if you have
            0b001011
        then it would mean that only channel 0, 1 and 3 are switched to on, the
        others are off.
        Helper method for write_pulse_form.
        """
        bits = 0  # that corresponds to: 0b0
        active_channels = np.where(active_channels == True)
        for channel in active_channels[0]:
            # go through each list element and create the digital word out of
            # 0 and 1 that represents the channel configuration. In order to do
            # that a bitwise shift to the left (<< operator) is performed and
            # the current channel configuration is compared with a bitwise OR
            # to check whether the bit was already set. E.g.:
            #   0b1001 | 0b0110: compare elementwise:
            #           1 | 0 => 1
            #           0 | 1 => 1
            #           0 | 1 => 1
            #           1 | 1 => 1
            #                   => 0b1111
            bits = bits | (1 << channel)
        return bits

    def  _convert_to_bitmask(self, active_channels): ## Modified by E.B
        """ Convert a list of channels into a bitmask.
        @param numpy.array active_channels: the list of active channels like
                            e.g. [0,4,7]. Note that the channels start from 0.
        @return int: The channel-list is converted into a bitmask (an sequence
                     of 1 and 0). The returned integer corresponds to such a
                     bitmask.
        Note that you can get a binary representation of an integer in python
        if you use the command bin(<integer-value>). All higher unneeded digits
        will be dropped, i.e. 0b00100 is turned into 0b100. Examples are
            bin(0) =    0b0
            bin(1) =    0b1
            bin(8) = 0b1000
        Each bit value (read from right to left) corresponds to the fact that a
        channel is on or off. I.e. if you have
            0b001011
        then it would mean that only channel 0, 1 and 3 are switched to on, the
        others are off.
        Helper method for write_pulse_form.
        """
        bits = 0     # that corresponds to: 0b0
        for channel in active_channels:
            # go through each list element and create the digital word out of
            # 0 and 1 that represents the channel configuration. In order to do
            # that a bitwise shift to the left (<< operator) is performed and
            # the current channel configuration is compared with a bitwise OR
            # to check whether the bit was already set. E.g.:
            #   0b1001 | 0b0110: compare elementwise:
            #           1 | 0 => 1
            #           0 | 1 => 1
            #           0 | 1 => 1
            #           1 | 1 => 1
            #                   => 0b1111
            bits = bits | (1<< channel)
        return bits



    def upload_asset(self, asset_name=None):
        """ Upload an already hardware conform file to the device.
            Does NOT load it into channels.

        @param name: string, name of the ensemble/seqeunce to be uploaded

        @return int: error code (0:OK, -1:error)
        """
        self.log.debug('PulseStreamer has no own storage capability.\n"upload_asset" call ignored.')
        return 0

    def tell(self, command):
        """ Sends a command string to the device.

        @param string command: string containing the command

        @return int: error code (0:OK, -1:error)
        """
        return 0

    def ask(self, question):
        """ Asks the device a 'question' and receive and return an answer from it.
        @param string question: string containing the command

        @return string: the answer of the device to the 'question' in a string
        """
        return ''

    def _get_dir_for_name(self, name):
        """ Get the path to the pulsed sub-directory 'name'.

        @param name: string, name of the folder
        @return: string, absolute path to the directory with folder 'name'.
        """
        path = os.path.join(self.pulsed_file_dir, name)
        if not os.path.exists(path):
            os.makedirs(os.path.abspath(path))
        return os.path.abspath(path)



















