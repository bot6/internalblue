#!/usr/bin/python2

# brcm_bt.py
#
# This is a helper module for debugging and reversing Broadcom Bluetooth chips.
# It requires a smartphone with compatible BCM chip and patched bluetooth stack
# which is connected via adb. Also pwntools must be installed.
# Features include dumping and manipulating memory in various ways.
#
# The tool is modular and allows adding new commands in a simple way (see cmds.py)
# HCI code was partially taken from https://github.com/joekickass/python-btsnoop
#
# Copyright (c) 2017 Dennis Mantz. (MIT License)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
# - The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
# - The Software is provided "as is", without warranty of any kind, express or
#   implied, including but not limited to the warranties of merchantability,
#   fitness for a particular purpose and noninfringement. In no event shall the
#   authors or copyright holders be liable for any claim, damages or other
#   liability, whether in an action of contract, tort or otherwise, arising from,
#   out of or in connection with the Software or the use or other dealings in the
#   Software.


from pwn import *
import socket
import time
import datetime
import Queue
import random

import hci
import fw

class BrcmBt():

    def __init__(self, queue_size=1000, btsnooplog_filename='btsnoop.log', log_level='info', fix_binutils='True'):
        context.log_level = log_level
        context.log_file = '_brcm_bt.log'
        context.arch = "thumb"
        self.hciport = None
        self.s_inject = None
        self.s_snoop = None
        if btsnooplog_filename != None:
            self.write_btsnooplog = True
            self.btsnooplog_file = open(btsnooplog_filename, "wb")
        else:
            self.write_btsnooplog = False
        self.recvQueue = Queue.Queue(queue_size)
        self.sendThreadrecvQueue = Queue.Queue(queue_size)
        self.sendQueue = Queue.Queue(queue_size)
        self.recvThread = None
        self.sendThread = None
        self.monitorThread = None
        self.monitorCallback = None
        self.exit_requested = False
        self.monitor_exit_requested = False
        self.running = False
        self.log_level = log_level
        self.check_binutils(fix_binutils)

    def check_binutils(self, fix=True):
        # Test if arm binutils is in path so that asm and disasm work:
        saved_loglevel = context.log_level
        context.log_level = 'critical'
        try:
            pwnlib.asm.which_binutils('as')
            context.log_level = saved_loglevel
            return True
        except PwnlibException:
            context.log_level = saved_loglevel
            log.debug("pwnlib.asm.which_binutils() cannot find 'as'!")
            if not fix:
                return False

        # Work around for arch (with installed arm-none-eabi-binutils)
        import os
        from glob import glob
        def which_binutils_fixed(tool):
            pattern = "arm-*-%s" % tool
            for directory in os.environ['PATH'].split(':'):
                res = sorted(glob(os.path.join(directory, pattern)))
                if res:
                    return res[0]
            raise PwnlibException("Could not find tool %s." % tool)

        try:
            which_binutils_fixed('as')
            # yeay it worked! fix it in pwnlib:
            pwnlib.asm.which_binutils = which_binutils_fixed
            log.debug("installing workaround for pwnlib.asm.which_binutils() ...")
            return True
        except PwnlibException:
            log.warn("pwntools cannot find binutils for arm architecture. Disassembing will not work!")
            return False

    def _read_btsnoop_hdr(self):
        data = self.s_snoop.recv(16)
        if(len(data) < 16):
            return None
        if(self.write_btsnooplog):
            self.btsnooplog_file.write(data)

        btsnoop_hdr = (data[:8], u32(data[8:12]),u32(data[12:16]))
        log.debug("BT Snoop Header: %s, version: %d, data link type: %d" % btsnoop_hdr)
        return btsnoop_hdr

    def _parse_time(self, time):
        """
        Record time is a 64-bit signed integer representing the time of packet arrival,
        in microseconds since midnight, January 1st, 0 AD nominal Gregorian.

        In order to avoid leap-day ambiguity in calculations, note that an equivalent
        epoch may be used of midnight, January 1st 2000 AD, which is represented in
        this field as 0x00E03AB44A676000.
        """
        time_betw_0_and_2000_ad = int("0x00E03AB44A676000", 16)
        time_since_2000_epoch = datetime.timedelta(microseconds=time) - datetime.timedelta(microseconds=time_betw_0_and_2000_ad)
        return datetime.datetime(2000, 1, 1) + time_since_2000_epoch

    def _recvThreadFunc(self):
        log.debug("Receive Thread started.")

        stackDumpReceiver = hci.StackDumpReceiver()

        while not self.exit_requested:
            # Little bit ugly: need to re-apply changes to the global context to the thread-copy
            context.log_level = self.log_level

            record_hdr = b''
            while(not self.exit_requested and len(record_hdr) < 24):
                try:
                    recv_data = self.s_snoop.recv(24 - len(record_hdr))
                    if len(recv_data) == 0:
                        log.info("recvThreadFunc: bt_snoop socket was closed by remote site. stopping recv thread...")
                        self.exit_requested = True
                        break
                    record_hdr += recv_data
                except socket.timeout:
                    pass # this is ok. just try again without error

            if not record_hdr or len(record_hdr) != 24:
                if not self.exit_requested:
                    log.warn("recvThreadFunc: Cannot recv record_hdr. stopping.")
                    self.exit_requested = True
                break

            if(self.write_btsnooplog):
                self.btsnooplog_file.write(record_hdr)

            orig_len, inc_len, flags, drops, time64 = struct.unpack( ">IIIIq", record_hdr)

            record_data = b''
            while(not self.exit_requested and len(record_data) < inc_len):
                try:
                    recv_data = self.s_snoop.recv(inc_len - len(record_data))
                    if len(recv_data) == 0:
                        log.info("recvThreadFunc: bt_snoop socket was closed by remote site. stopping..")
                        self.exit_requested = True
                        break
                    record_data += recv_data
                except socket.timeout:
                    pass # this is ok. just try again without error

            if not record_data or len(record_data) != inc_len:
                if not self.exit_requested:
                    log.warn("recvThreadFunc: Cannot recv data. stopping.")
                    self.exit_requested = True
                break
            
            if(self.write_btsnooplog):
                self.btsnooplog_file.write(record_data)

            try:
                parsed_time = self._parse_time(time64)
            except OverflowError:
                parsed_time = None

            record = (hci.parse_hci_packet(record_data), orig_len, inc_len, flags, drops, parsed_time)

            log.debug("Recv: [" + str(parsed_time) + "] " + str(record[0]))

            if(record != None):
                if self.recvQueue.full():
                    log.debug("recvThreadFunc: recv queue is full. flushing..")
                    try:
                        while True:
                            self.recvQueue.get(block=False)
                    except Queue.Empty:
                        pass

                try:
                    self.recvQueue.put(record, block=False)
                except Queue.Full:
                    log.warn("recvThreadFunc: recv queue is full. dropping packets..")

                if self.sendThread != None and self.sendThread.isAlive():
                    try:
                        self.sendThreadrecvQueue.put(record, block=False)
                    except Queue.Full:
                        log.warn("recvThreadFunc: sendThread recv queue is full. dropping packets..")

                if stackDumpReceiver.recvPacket(record[0]):
                    # A stack dump has happend!
                    log.warn("recvThreadFunc: The controller send a stack dump. stopping..")
                    self.exit_requested = True

        log.debug("Receive Thread terminated.")

    def _sendThreadFunc(self):
        log.debug("Send Thread started.")
        while not self.exit_requested:
            # Little bit ugly: need to re-apply changes to the global context to the thread-copy
            context.log_level = self.log_level

            # flushing recv queue to prevent it from filling up...
            try:
                while True:
                    self.sendThreadrecvQueue.get(block=False)
            except Queue.Empty:
                pass

            # Wait for packet in send queue
            try:
                task = self.sendQueue.get(timeout=0.5)
            except Queue.Empty:
                continue

            opcode, data, queue = task
            payload = p16(opcode) + p8(len(data)) + data

            # Prepend UART TYPE and length
            out = p8(hci.HCI.HCI_CMD) + p16(len(payload)) + payload
            log.debug("_sendThreadFunc: Send: " + str(out.encode('hex')))
            self.s_inject.send(out)

            while not self.exit_requested:
                # Receive response
                packet = self.recvPacket(timeout=0.5)
                if packet == None:
                    continue
                hcipkt, orig_len, inc_len, flags, drops, recvtime = packet

                if isinstance(hcipkt, hci.HCI_Event):
                    if hcipkt.event_code == 0x0e: # Cmd Complete event
                        if hcipkt.data[1:3] == p16(opcode):
                            queue.put(hcipkt.data)
                            break

        log.debug("Send Thread terminated.")

    def _monitorThreadFunc(self):
        log.debug("monitorThread: started!")

        HOOK_BASE_ADDRESS = 0xd7600
        DATA_BASE_ADDRESS = 0xd7700
        DATA_LENGTH = 32*32+4
        MAX_WAITTIME = 0.5
        INJECTED_CODE = """
            b hook_send_lmp
            b hook_recv_lmp

            hook_recv_lmp:
                push {r2-r8,lr}
                push {r0-r3,lr}

                // inc counter
                ldr  r0, =0x%x
                ldr  r1, [r0]
                add  r1, 1
                str  r1, [r0]

                // calc table offset = (cnt & 0b11111) << 5
                and  r2, r1, 0x1F
                lsl  r2, r2, 5
                // store counter at table entry
                add  r0, 4
                add  r0, r2
                str  r1, [r0]

                // read data
                add  r0, 4
                ldr  r1, =0x200478
                ldr  r2, [r1]
                str  r2, [r0]
                add  r0, 4
                add  r1, 4
                ldr  r1, [r1]
                add  r1, 0xC    // start of LMP packet

                mov  r2, 24
                bl   0x2e03c+1  // memcpy

                pop  {r0-r3,lr}
                b    0x3F3F8

            hook_send_lmp:
                push {r4,r5,lr}

                // save parameters
                mov  r5, r0 // conn struct
                mov  r4, r1 // buffer

                // inc counter
                ldr  r0, =0x%x
                ldr  r1, [r0]
                add  r1, 1
                str  r1, [r0]

                // calc table offset = (cnt & 0b11111) << 5
                and  r2, r1, 0x1F
                lsl  r2, r2, 5
                // store counter at table entry
                mov  r3, 1
                orr.w r1, r1, r3, LSL#31
                add  r0, 4
                add  r0, r2
                str  r1, [r0]

                // get connection number
                add  r0, 6
                ldr  r2, [r5]
                strb r2, [r0]
                add  r0, 2
                // read data
                add  r1, r4, 0xC    // start of LMP packet

                mov  r2, 24
                bl   0x2e03c+1  // memcpy

                mov r0, 0
                pop  {r4,r5,pc}
            """ % (DATA_BASE_ADDRESS, DATA_BASE_ADDRESS)

        # Injecting hooks
        hooks_code = asm(INJECTED_CODE, vma=HOOK_BASE_ADDRESS)
        saved_data_hooks = self.readMem(HOOK_BASE_ADDRESS, len(hooks_code))
        saved_data_data  = self.readMem(DATA_BASE_ADDRESS, DATA_LENGTH)
        self.writeMem(DATA_BASE_ADDRESS, p32(0))
        log.debug("monitorThread: injecting hook functions...")
        self.writeMem(HOOK_BASE_ADDRESS, hooks_code)
        log.debug("monitorThread: inserting lmp send hook ...")
        self.writeMem(fw.LMP_SEND_PACKET_HOOK, p32(HOOK_BASE_ADDRESS + 1))
        log.debug("monitorThread: inserting lmp recv hook ...")
        recv_patch_handle = self.patchRom(0x3f3f4, asm("b 0x%x" % (HOOK_BASE_ADDRESS + 5), vma=0x3f3f4))
        log.debug("monitorThread: monitor mode activated.")

        # Get device's BT address
        deviceAddress = self.readMem(fw.BD_ADDR, 6)

        # Poll data
        lastCapturedIndex = 0
        waittime = MAX_WAITTIME
        while not self.exit_requested and not self.monitor_exit_requested:
            # Little bit ugly: need to re-apply changes to the global context to the thread-copy
            context.log_level = self.log_level
            currentCapturedIndex = u32(self.readMem(DATA_BASE_ADDRESS, 4)) & 0x7FFFFFFF
            if currentCapturedIndex <= lastCapturedIndex:
                time.sleep(waittime)
                if waittime < MAX_WAITTIME:
                    waittime += MAX_WAITTIME/10
                continue
            else:
                waittime = 0

            currentCapturedPosition = currentCapturedIndex & 0x1f
            lastCapturedPosition = lastCapturedIndex & 0x1f
            if currentCapturedIndex - lastCapturedIndex >= 32:
                # all entries are new (maybe even dropped packets)
                data = self.readMem(DATA_BASE_ADDRESS+4, 32*32)
            elif lastCapturedPosition < currentCapturedPosition:
                # no wrap around
                data = self.readMem(DATA_BASE_ADDRESS+4+
                        (lastCapturedPosition+1)*32,
                        (currentCapturedPosition-lastCapturedPosition)*32)
            else:
                # wrap around
                tmp = self.readMem(DATA_BASE_ADDRESS+4+
                        (lastCapturedPosition+1)*32,
                        (31-lastCapturedPosition)*32)
                data = self.readMem(DATA_BASE_ADDRESS+4,
                        (currentCapturedPosition+1)*32) + tmp

            entries = [(u32(data[i:i+4]), data[i+4:i+32]) for i in range(0, len(data), 32)]
            entries.sort(key=lambda x: x[0] & 0x7FFFFFFF)
            log.info("lastCapturedIndex=%d, currentCapturedIndex=%d, entries=%s" % (lastCapturedIndex, currentCapturedIndex, str(entries)))
            #log.hexdump(self.readMem(DATA_BASE_ADDRESS, DATA_LENGTH))
            connection_numbers = set([ord(entry[1][2]) for entry in entries])
            connection_addresses = {}
            for number in connection_numbers:
                connection_addresses[number] = self.readMem(fw.CONNECTION_ARRAY_ADDRESS + 
                        (number-1)*fw.CONNECTION_STRUCT_LENGTH + 0x28, 6)[::-1]
            log.info("connection_addresses = " + str(connection_addresses))
            for entry in entries:
                sendFromDevice = entry[0] & 0x80000000 > 0
                connection_number = ord(entry[1][2])
                index = entry[0] & 0x7FFFFFFF
                lmp_opcode = u8(entry[1][4]) >> 1
                if lmp_opcode >= 0x7C:
                    lmp_opcode = u8(entry[1][5])
                    lmp_len = fw.LMP_ESC_LENGTHS[lmp_opcode]
                else:
                    lmp_len = fw.LMP_LENGTHS[lmp_opcode]
                lmp_packet = entry[1][4:4+lmp_len]
                if index > lastCapturedIndex + 1:
                    log.warn("monitorThread: Dropped %d packets!" % (index-lastCapturedIndex-1))
                if index <= lastCapturedIndex:
                    log.warn("monitorThread: Error, got an old packet (index=%d but we are at %d)" % (index, lastCapturedIndex))
                src_addr = deviceAddress if sendFromDevice else connection_addresses[connection_number]
                dest_addr = deviceAddress if not sendFromDevice else connection_addresses[connection_number]
                self.monitorCallback(lmp_packet, sendFromDevice, src_addr, dest_addr)
                lastCapturedIndex = index

        # Removing hooks
        log.debug("monitorThread: removing lmp send hook ...")
        self.writeMem(fw.LMP_SEND_PACKET_HOOK, p32(0))
        log.debug("monitorThread: removing lmp recv hook ...")
        self.disableRomPatch(recv_patch_handle)
        log.debug("monitorThread: Restoring saved data...")
        self.writeMem(HOOK_BASE_ADDRESS, saved_data_hooks)
        self.writeMem(DATA_BASE_ADDRESS, saved_data_data)

        log.debug("monitorThread: terminated!")


    def _setupSockets(self):
        self.hciport = random.randint(60000, 65535)     # select a random port (and hope that it is not in use)
        log.debug("_setupSockets: Selected random ports snoop=%d and inject=%d" % (self.hciport, self.hciport+1))

        saved_loglevel = context.log_level
        context.log_level = 'warn'
        try:
            adb.adb(["forward", "tcp:%d"%(self.hciport),   "tcp:8872"])
            adb.adb(["forward", "tcp:%d"%(self.hciport+1), "tcp:8873"])
        except PwnlibException as e:
            log.warn("Setup adb port forwarding failed: " + str(e))
            return False
        finally:
            context.log_level = saved_loglevel
        
        # Connect to hci injection port
        self.s_inject = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s_inject.connect(('127.0.0.1', self.hciport+1))
        self.s_inject.settimeout(0.5)

        # Connect to hci snoop log port
        self.s_snoop = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s_snoop.connect(('127.0.0.1', self.hciport))
        self.s_snoop.settimeout(0.5)

        # Read btsnoop header
        if(self._read_btsnoop_hdr() == None):
            log.warn("Could not read btsnoop header")
            self.s_inject.close()
            self.s_snoop.close()
            self.s_inject = self.s_snoop = None
            adb.adb(["forward", "--remove", "tcp:%d"%(self.hciport)])
            adb.adb(["forward", "--remove", "tcp:%d"%(self.hciport+1)])
            return False
        return True

    def _teardownSockets(self):
        if(self.s_inject != None):
            self.s_inject.close()
            self.s_inject = None
        if(self.s_snoop != None):
            self.s_snoop.close()
            self.s_snoop = None

        saved_loglevel = context.log_level
        context.log_level = 'warn'
        try:
            adb.adb(["forward", "--remove", "tcp:%d"%(self.hciport)])
            adb.adb(["forward", "--remove", "tcp:%d"%(self.hciport+1)])
        except PwnlibException as e:
            log.warn("Removing adb port forwarding failed: " + str(e))
            return False
        finally:
            context.log_level = saved_loglevel

    def check_running(self):
        if self.exit_requested:
            self.shutdown()

        if not self.running:
            log.warn("Not running. call connect() first!")
            return False
        return True

    def connect(self):
        if self.exit_requested:
            self.shutdown()

        if self.running:
            log.warn("Already running. call shutdown() first!")
            return False

        # Check for connected adb devices
        adb_devices = adb.devices()
        if(len(adb_devices) == 0):
            log.critical("No adb devices found.")
            return False
        if(len(adb_devices) > 1):
            log.info("Found multiple adb devices. Please specify!")
            choice = options("Please choose:", [d.serial + ' (' + d.model + ')' for d in adb_devices])
            context.device = adb_devices[choice].serial
        else:
            log.info("Using adb device: %s (%s)" % (adb_devices[0].serial, adb_devices[0].model))
            context.device = adb_devices[0].serial

        # setup sockets
        if not self._setupSockets():
            log.critical("No connection to target device.")
            return False

        # start receive thread
        self.recvThread = context.Thread(target=self._recvThreadFunc)
        self.recvThread.setDaemon(True)
        self.recvThread.start()

        # start send thread
        self.sendThread = context.Thread(target=self._sendThreadFunc)
        self.sendThread.setDaemon(True)
        self.sendThread.start()

        self.running = True
        return True

    def shutdown(self):
        self.exit_requested = True
        self.recvThread.join()
        self.sendThread.join()
        self._teardownSockets()
        if(self.write_btsnooplog):
            self.btsnooplog_file.close()
        self.running = False
        self.exit_requested = False
        log.info("Shutdown complete.")

    def startMonitor(self, callback):
        if not self.check_running():
            return False
        if self.monitorThread != None:
            log.warning("startMonitor: monitor thread does already exist")
            return False

        self.monitor_exit_requested = False
        self.monitorCallback = callback
        self.monitorThread = context.Thread(target=self._monitorThreadFunc)
        self.monitorThread.setDaemon(True)
        self.monitorThread.start()
        return True

    def stopMonitor(self):
        self.monitor_exit_requested = True
        if threading.currentThread() != self.monitorThread:
            log.debug("stopMonitor: Waiting on Monitor Thread to terminate...")
            self.monitorThread.join()
        else:
            log.debug("stopMonitor: Called from monitor thread. skip joining.")
        self.monitorThread = None
        log.debug("stopMonitor: Monitor Thread terminated.")

    def sendHciCommand(self, opcode, data, timeout=2):
        queue = Queue.Queue(1)
        try:
            self.sendQueue.put((opcode, data, queue), timeout=timeout)
            return queue.get(timeout=timeout)
        except Queue.Empty:
            log.warn("sendHciCommand: waiting for response timed out!")
            return None
        except Queue.Full:
            log.warn("sendHciCommand: send queue is full!")
            return None

    def recvPacket(self, timeout=None):
        if not self.check_running():
            return None

        try:
            if self.sendThread == threading.currentThread():
                return self.sendThreadrecvQueue.get(timeout=timeout)
            else:
                return self.recvQueue.get(timeout=timeout)
        except Queue.Empty:
            return None

    def readMem(self, address, length, progress_log=None, bytes_done=0, bytes_total=0):
        if not self.check_running():
            return None

        read_addr = address
        byte_counter = 0
        outbuffer = ''
        memory_type = None
        if bytes_total == 0:
            bytes_total = length
        while(read_addr < address+length):
            # Send hci frame
            bytes_left = length - byte_counter
            blocksize = bytes_left
            if blocksize > 251:
                blocksize = 251

            response = self.sendHciCommand(0xfc4d, p32(read_addr) + p8(blocksize))

            status = ord(response[3])
            if status != 0:
                log.warning("readMem: [TODO] Got status != 0 : 0x%02X" % status)
            data = response[4:]
            outbuffer += data
            read_addr += len(data)
            byte_counter += len(data)
            if(progress_log != None):
                msg = "receiving data... %d / %d Bytes (%d%%)" % (bytes_done+byte_counter, 
                        bytes_total, (bytes_done+byte_counter)*100/bytes_total)
                progress_log.status(msg)
        return outbuffer

    def writeMem(self, address, data, progress_log=None, bytes_done=0, bytes_total=0):
        if not self.check_running():
            return None

        write_addr = address
        byte_counter = 0
        if bytes_total == 0:
            bytes_total = len(data)
        while(byte_counter < len(data)):
            # Send hci frame
            bytes_left = len(data) - byte_counter
            blocksize = bytes_left
            if blocksize > 251:
                blocksize = 251

            response = self.sendHciCommand(0xfc4c, p32(write_addr) + data[byte_counter:byte_counter+blocksize])
            if(response[3] != '\x00'):
                log.warn("Got error code %x in command complete event." % response[3])
                return False
            write_addr += blocksize
            byte_counter += blocksize
            if(progress_log != None):
                msg = "sending data... %d / %d Bytes" % (bytes_done+byte_counter, bytes_total)
                progress_log.status(msg)
        return True

    def launchRam(self, address):
        response = self.sendHciCommand(0xfc4e, p32(address))

        if(response[3] != '\x00'):
            log.warn("Got error code %x in command complete event." % response[3])
            return False
        return True

    def patchRom(self, address, patch):
        if len(patch) != 4:
            log.warn("patchRom: patch must be a 32-bit dword!")
            return False

        # Not so nice hack to keep track of used slots:
        # TODO: This can be better by reading in the bitfields from the IO
        # This needs a patch as readRAM crashes if it reads from IO (must read 4 byte chunks)
        slot_dwords = [0xffffffff, 0xffffffff, 0xffffffff, 0x0000ffff, 0x00000000]
        slot = 113

        # We need to enable the slot by setting a bit in a multi-dword bitfield
        target_dword = int(slot / 32)
        target_bit = slot % 32

        if slot_dwords[target_dword] & (0b1 << target_bit):
            log.warn("Slot %d is already in use. Overwriting..." % slot)

        slot_dwords[target_dword] |= 0b1 << target_bit

        # Write new value to patchram value table at 0xd0000
        self.writeMem(0xd0000 + slot*4, patch)

        # Write address to patchram target table at 0x31000
        self.writeMem(0x310000 + slot*4, p32(address >> 2))

        # Enable patchram slot (enable bitfield starts at 0x310204)
        self.writeMem(0x310204 + target_dword*4, p32(slot_dwords[target_dword]))
        return True

    def disableRomPatch(self, patchIndex):
        #TODO
        pass

    def readConnectionInformation(self, conn_number):
        if conn_number < 1 or conn_number > fw.CONNECTION_ARRAY_SIZE:
            log.warn("readConnectionInformation: connection number out of bounds: %d" % conn_number)
            return None

        connection = self.readMem(fw.CONNECTION_ARRAY_ADDRESS +
                            fw.CONNECTION_STRUCT_LENGTH*(conn_number-1),
                            fw.CONNECTION_STRUCT_LENGTH)

        if connection == b'\x00'*fw.CONNECTION_STRUCT_LENGTH:
            return None

        conn_dict = {}
        conn_dict["connection_number"]   = u32(connection[:4])
        conn_dict["remote_address"]      = connection[0x28:0x2E][::-1]
        conn_dict["remote_name_address"] = u32(connection[0x4C:0x50])
        conn_dict["master_of_connection"] = u32(connection[0x1C:0x20]) & 1<<15 != 0
        return conn_dict

    def sendLmpPacket(self, conn_nr, opcode, payload, extended_op=False):
        if conn_nr < 1 or conn_nr > fw.CONNECTION_ARRAY_SIZE:
            log.warn("sendLmpPacket: connection number out of bounds: %d" % conn_nr)
            return False

        connection = self.readConnectionInformation(conn_nr)
        tid = 1 if connection["master_of_connection"] else 0
        opcode_data = p8(opcode<<1 | tid) if not args.ext else p8(0x7F<<1|tid) + p8(opcode)
        data = opcode_data + payload

        CODE_BASE_ADDRESS = 0xd7500
        DATA_BASE_ADDRESS = 0xd7580
        ASM_CODE = """
                push {r4,lr}

                // malloc buffer
                bl 0x3F17E      // malloc_0x20_bloc_buffer_memzero
                mov r4, r0

                // fill buffer
                add r0, 0xC
                ldr r1, =0x%x
                mov r2, 20
                bl  0x2e03c     // memcpy

                // load conn struct pointer
                mov r0, %d
                bl 0x42c04      // find connection struct from conn nr

                mov r1, r4
                pop {r4,lr}
                b 0xf81a        // send_LMP_packet
                """ % (DATA_BASE_ADDRESS, conn_nr)

        code = asm(ASM_CODE, vma=CODE_BASE_ADDRESS)
        self.writeMem(CODE_BASE_ADDRESS, code)
        self.writeMem(DATA_BASE_ADDRESS, data.ljust(20, "\x00"))

        if self.launchRam(CODE_BASE_ADDRESS):
            return True
        else:
            log.warn("sendLmpPacket: launchRam failed!")
            return False
