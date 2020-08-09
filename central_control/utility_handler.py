#!/usr/bin/env python3
import paho.mqtt.client as mqtt
import argparse
import pickle
import threading, queue
import central_control.us as us
import central_control.pcb as pcb
import central_control.motion as motion
from central_control.illumination import illumination
import logging
from collections.abc import Iterable
import pyvisa

# for storing command messages as they arrive
cmdq = queue.Queue()

# for storing jobs to be worked on
taskq = queue.Queue()

# for outgoing messages
outputq = queue.Queue()

# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):
    print("Connected with result code "+str(rc))

    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    client.subscribe("cmd/#", qos=2)


# The callback for when a PUBLISH message is received from the server.
# this function must be fast and non-blocking to avoid estop service delay
def handle_message(client, userdata, msg):
    cmdq.put_nowait(msg)  # pass this off for our worker to deal with


# filters out all mqtt messages except
# properly formatted command messages, unpacks and returns those
# this function must be fast and non-blocking to avoid estop service delay
def filter_cmd(mqtt_msg):
    result = {'cmd':''}
    try:
        msg = pickle.loads(mqtt_msg.payload)
    except:
        msg = None
    if isinstance(msg, Iterable):
        if 'cmd' in msg:
            result = msg
    return(result)


# the manager thread decides if the command should be passed on to the worker or rejected.
# immediagely handles estops itself
# this function must be fast and non-blocking to avoid estop service delay
def manager():
    while True:
        cmd_msg = filter_cmd(cmdq.get())
        if cmd_msg['cmd'] == 'estop':
            try:
                with pcb.pcb(cmd_msg['pcb'], timeout=10) as p:
                    p.get('b')
                log_msg('Emergency stop done. Re-Homing required before any further movements.', lvl=logging.INFO)
            except:
                log_msg(f'Unable to complete task.', lvl=logging.WARNING)
        elif (taskq.unfinished_tasks == 0):
            # the worker is available so let's give it something to do
            taskq.put_nowait(cmd_msg)
        else:
            log_msg('Backend busy. Command rejected.', lvl=logging.WARNING)
        cmdq.task_done()

# asks for the current stage position and sends it up to /response
def get_stage(pcba, uri):
    with pcb.pcb(pcba, timeout=1) as p:
        mo = motion.motion(address=uri, pcb_object=p)
        mo.connect()
        pos = mo.get_position()
    payload = {'pos': pos}
    payload = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    output = {'destination':'response', 'payload': payload}  # post the position to the response channel
    outputq.put(output)

# work gets done here so that we don't do any processing on the mqtt network thread
# can block and be slow. new commands that come in while this is working will be rejected
def worker():
    while True:
        task = taskq.get()
        try:
            if task['cmd'] == 'home':
                with pcb.pcb(task['pcb'], timeout=1) as p:
                    mo = motion.motion(address=task['stage_uri'], pcb_object=p)
                    mo.connect()
                    result = mo.home()
                    if isinstance(result, list) or (result == 0):
                        log_msg('Homing procedure complete.',lvl=logging.INFO)
                        get_stage(task['pcb'], task['stage_uri'])
                    else:
                        log_msg(f'Home failed with result {result}',lvl=logging.WARNING)

            # send the stage some place
            elif task['cmd'] == 'goto':
                with pcb.pcb(task['pcb'], timeout=1) as p:
                    mo = motion.motion(address=task['stage_uri'], pcb_object=p)
                    mo.connect()
                    result = mo.goto(task['pos'])
                    if result != 0:
                        log_msg(f'GOTO failed with result {result}',lvl=logging.WARNING)
                    get_stage(task['pcb'], task['stage_uri'])

            # handle any generic PCB command that has an empty return on success
            elif task['cmd'] == 'for_pcb':
                with pcb.pcb(task['pcb'], timeout=1) as p:
                    # special case for pixel selection to avoid parallel connections
                    if (task['pcb_cmd'].startswith('s') and ('stream' not in task['pcb_cmd']) and (len(task['pcb_cmd']) != 1)):
                        p.get('s')  # deselect all before selecting one
                    result = p.get(task['pcb_cmd'])
                if result == '':
                    log_msg(f"Command acknowledged: {task['pcb_cmd']}",lvl=logging.DEBUG)
                else:
                    log_msg(f"Command {task['pcb_cmd']} not acknowleged with {result}",lvl=logging.WARNING)

            # get the stage location
            elif task['cmd'] == 'read_stage':
                get_stage(task['pcb'], task['stage_uri'])

            # zero the mono
            elif task['cmd'] == 'mono_zero':
                try:
                    rm = pyvisa.ResourceManager()
                    with rm.open_resource(task['mono_address'], baud_rate=9600) as mono:
                        log_msg(mono.query("0 GOTO").strip(), lvl=logging.INFO)
                        log_msg(mono.query("1 FILTER").strip(), lvl=logging.INFO)
                except:
                    log_msg(f'Unable to zero Monochromator',lvl=logging.WARNING)

            # device round robin commands
            elif task['cmd'] == 'round_robin':
                with pcb.pcb(task['pcb'], timeout=1) as p:
                    p.get('s') # make sure we're starting with nothing selected
                    if task['type'] == 'current':
                        pass  # TODO: smu measure current command goes here
                    for sel in task['devices']:
                        p.get(sel)  # select the device
                        if task['type'] == 'current':
                            pass  # TODO: smu measure current command goes here
                        elif task['type'] == 'rtd':
                            pass  # TODO: smu resistance command goes here
                        elif task['type'] == 'connectivity':
                            pass  # TODO: smu connectivity command goes here
                        p.get('s') # deselect the device


                    #mo = motion.motion(address=task['stage_uri'], pcb_object=p)
                    #mo.connect()
                    #pos = mo.get_position()
                    
                #payload = {'pos': pos}
                #payload = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
                #output = {'destination':'response', 'payload': payload}  # post the position to the response channel
                #outputq.put(output)
        except:
            log_msg(f'Unable to complete task.',lvl=logging.WARNING)

        # system health check
        if task['cmd'] == 'check_health':
            log_msg(f"Checking controller@{task['pcb']}...",lvl=logging.INFO)
            try:
                with pcb.pcb(task['pcb'], timeout=1) as p:
                    log_msg('Controller connection initiated',lvl=logging.INFO)
                    log_msg(f"Controller firmware version: {p.get('v')}",lvl=logging.INFO)
                    log_msg(f"Controller stage bitmask value: {p.get('e')}",lvl=logging.INFO)
                    log_msg(f"Controller mux bitmask value: {p.get('c')}",lvl=logging.INFO)
            except:
                log_msg(f'Could not talk to control box',lvl=logging.WARNING)

            log_msg(f"Checking power supply@{task['psu']}...",lvl=logging.INFO)
            rm = pyvisa.ResourceManager()
            try:
                with rm.open_resource(task['psu']) as psu:
                    log_msg('Power supply connection initiated',lvl=logging.INFO)
                    idn = psu.query("*IDN?")
                    log_msg(f'Power supply identification string: {idn.strip()}',lvl=logging.INFO)
            except:
                log_msg(f'Could not talk to PSU',lvl=logging.WARNING)
            
            log_msg(f"Checking sourcemeter@{task['smu_address']}...",lvl=logging.INFO)
            
            # for sourcemeter
            open_params = {}
            open_params['resource_name'] = task['smu_address']
            open_params['timeout'] = 300 # ms
            if 'ASRL' in open_params['resource_name']:  # data bits = 8, parity = none
                open_params['read_termination'] = task['smu_le']  # NOTE: <CR> is "\r" and <LF> is "\n" this is set by the user by interacting with the buttons on the instrument front panel
                open_params['write_termination'] = "\r" # this is not configuable via the instrument front panel (or in any way I guess)
                open_params['baud_rate'] = task['smu_baud']  # this is set by the user by interacting with the buttons on the instrument front panel
                open_params['flow_control'] = pyvisa.constants.VI_ASRL_FLOW_XON_XOFF # this must be set by the user by interacting with the buttons on the instrument front panel
            elif 'GPIB' in open_params['resource_name']:
                open_params['write_termination'] = "\n"
                # GPIB takes care of EOI, so there is no read_termination
                open_params['io_protocol'] = pyvisa.constants.VI_HS488  # this must be set by the user by interacting with the buttons on the instrument front panel by choosing 488.1, not scpi
            elif ('TCPIP' in open_params['resource_name']) and ('SOCKET' in open_params['resource_name']):
                # GPIB <--> Ethernet adapter
                pass

            try:
                with rm.open_resource(**open_params) as smu:
                    log_msg('Sourcemeter connection initiated',lvl=logging.INFO)
                    idn = smu.query("*IDN?")
                    log_msg(f'Sourcemeter identification string: {idn}',lvl=logging.INFO)
            except:
                log_msg(f'Could not talk to sourcemeter',lvl=logging.WARNING)
            
            log_msg(f"Checking lock-in@{task['lia_address']}...",lvl=logging.INFO)
            try:
                with rm.open_resource(task['lia_address'], baud_rate=9600) as lia:
                    lia.read_termination = '\r'
                    log_msg('Lock-in connection initiated',lvl=logging.INFO)
                    idn = lia.query("*IDN?")
                    log_msg(f'Lock-in identification string: {idn.strip()}',lvl=logging.INFO)
            except:
                log_msg(f'Could not talk to lock-in',lvl=logging.WARNING)
            
            log_msg(f"Checking monochromator@{task['mono_address']}...",lvl=logging.INFO)
            try:
                with rm.open_resource(task['mono_address'], baud_rate=9600) as mono:
                    log_msg('Monochromator connection initiated',lvl=logging.INFO)
                    qu = mono.query("?nm")
                    log_msg(f'Monochromator wavelength query result: {qu.strip()}',lvl=logging.INFO)
            except:
                log_msg(f'Could not talk to monochromator',lvl=logging.WARNING)

            log_msg(f"Checking light engine@{task['le_address']}...",lvl=logging.INFO)
            try:
                le = illumination(address=task['le_address'], default_recipe=task['le_recipe'])
                con_res = le.connect()
                le.light_engine.__del__()
                if con_res == 0:
                    log_msg('Light engine connection successful',lvl=logging.INFO)
                elif (con_res == -1):
                    log_msg("Timeout waiting for wavelabs to connect",lvl=logging.WARNING)
                else:
                    log_msg(f"Unable to connect to light engine and activate {task['le_recipe']} with error {con_res}",lvl=logging.WARNING)
            except:
                log_msg(f'Could not talk to light engine',lvl=logging.WARNING)

        taskq.task_done()


# send up a log message to the status channel
def log_msg(msg, lvl=logging.DEBUG):
    payload = {'log':{'level':lvl, 'text':msg}}
    payload = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    output = {'destination':'status', 'payload': payload}
    outputq.put(output)


# thread that publishes mqtt messages on behalf of the worker and manager
def sender(mqttc):
    while True:
        to_send = outputq.get()
        mqttc.publish(to_send['destination'], to_send['payload'], qos=2).wait_for_publish()
        outputq.task_done()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Handle gui stage commands')
    parser.add_argument('-a', '--address', type=str, default='127.0.0.1', help='ip address/hostname of the mqtt server')
    parser.add_argument('-p', '--port', type=int, default=1883, help="MQTT server port")
    args = parser.parse_args()

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = handle_message

    # start the manager (decides what to do with commands from mqtt)
    threading.Thread(target=manager, daemon=True).start()

    # start the worker (does tasks the manger tells it to)
    threading.Thread(target=worker, daemon=True).start()

    # connect to the mqtt server
    client.connect(args.address, port=args.port, keepalive=60)

    # start the sender (publishes messages from worker and manager)
    threading.Thread(target=sender, args=(client,)).start()

    # Blocking call that processes network traffic, dispatches callbacks and
    # handles reconnecting.
    # Other loop*() functions are available that give a threaded interface and a
    # manual interface.
    client.loop_forever()
