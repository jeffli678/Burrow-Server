import uuid
import base64
import multiprocessing
import Queue
import sys

from scapy import route
from scapy.layers.inet import IP
from scapy.layers.inet import TCP
from scapy.layers.inet import UDP
from scapy.all import sr

from burrow_logging import burrow_log
def LOG(s):
    burrow_log(s, 8)

NO_ERROR = 0
INVALID_PACKET = 1
NO_FREE_PORTS = 2

# Technically, DNS responses can be up to 64KB. We aren't looking to
# find that limit here, though. This would be a good value to experiment
# with optimizing.
MAX_RESPONSE_SIZE = 8000

SR_TIMEOUT = 60  # seconds

SERVER_IP = "131.215.172.230"
available_ports = range(30000,50000)     #ports will be removed from this list while in use
sessions = {}


def sizeof_list(l):
    size = 0
    for i in l:
        size += sys.getsizeof(i)
    size += sys.getsizeof(l)
    return size

class Session:
    def __init__(self, id):
        self.id = id
        self.pending_response_packets = multiprocessing.Queue()

    def request(self):
        response_packets = []
        while sizeof_list(response_packets) < MAX_RESPONSE_SIZE:
            try:
                r_pkt = self.pending_response_packets.get_nowait()
                response_packets.append(r_pkt)
            except Queue.Empty:
                break
        return response_packets

    def sendreceive_packet_with_timeout(self, secs, packet, original_src, original_sport, protocol, spoofed_sport):
        p = multiprocessing.Process(target=self.sendreceive_packet, args=(packet, original_src, original_sport, protocol, spoofed_sport,))
        p.start()
        p.join(secs)
        if p.is_alive():
            LOG("Warning: long-running sr process terminated.")
            p.terminate()
            p.join()

    def sendreceive_packet(self, packet, original_src, original_sport, protocol, spoofed_sport):
        #send packet and receive responses.
        #self.ans will contain a list of tuples of sent packets and their responses
        #self.unans will contain a list of unanswered packets
        LOG("About to forward packet for " + self.id)
        ans, unans = sr(packet, verbose=0)
        #un-spoof the source IP address and port,
        #then add to the list of packets waiting to be sent back
        for pair in ans:
            LOG("Received response packet for " + self.id)
            response = pair[1]
            response[IP].src = original_src
            response[protocol].sport = original_sport
            response = IP(str(response))    #recalculate all the checksums
            self.pending_response_packets.put(base64.b64encode(str(response)))
            LOG("Appended response packet, session " + self.id + " (" + str(id(self)) + ") now has " + str(self.pending_response_packets.qsize()) + " packets waiting to be pulled from list " + str(id(self.pending_response_packets)))
        available_ports.append(spoofed_sport)  #return port to available pool

    def forward(self, message):
        pkt = IP(message)        #parse the binary data to a scapy IP packet
        # pkt.show2()

        if IP not in pkt:
                    return INVALID_PACKET
        # LOG("Forwarding packet to IP address " + str(pkt[IP].dst))
        original_src = pkt[IP].src   #store the original source IP
        pkt[IP].src = SERVER_IP      #spoof the source IP so the packet comes back to us
        del pkt[IP].chksum           #invalidate the checksum
        if len(available_ports) == 0:
            return NO_FREE_PORTS
        port = available_ports.pop(0)        #get a port from our pool of available ports
        if TCP in pkt:
            protocol = TCP
            original_sport = pkt[TCP].sport  #store the original source port
            pkt[TCP].sport = port            #spoof the source port
            #pkt[TCP].dport = ____
            del pkt[TCP].chksum              #invalidate the checksum
        elif UDP in pkt:
            protocol = UDP
            original_sport = pkt[UDP].sport  #ditto
            pkt[UDP].sport = port
            #pkt[UDP].dport = ____
            del pkt[UDP].chksum
        else:
            return INVALID_PACKET

        pkt = IP(str(pkt))   #recalculate all the checksums

        # print "After spoofing, packet looks like:"
        # pkt.show2()

        p = multiprocessing.Process(target=self.sendreceive_packet_with_timeout, args=(SR_TIMEOUT, pkt, original_src, original_sport, protocol, port,))
        p.start()

        return NO_ERROR


def handle_message(message):
    response = ""
    components = iter(message.split('-'))
    type = components.next()
    if (type == 'b'):
        response = got_begin_session()
    elif (type == 'f'):
        response = got_forward_packets(components)
    elif (type == 'r'):
        response = got_request_packets(components)
    elif (type == 'e'):
        response = got_end_session(components)
    elif (type == 'test'):
        # reverse the string
        response = message[::-1]
        LOG("Session layer received test message, responding with " + response)
    else:
        # This should never happen
        response = "f-1-Message_type_`" + str(type) + "`_is_unkown."
    return response

def got_begin_session():
    session_id = uuid.uuid4().hex[-8:]
    sessions[session_id] = Session(session_id)
    LOG("Began session with id: " + str(session_id))
    return "s-" + str(session_id)

def got_forward_packets(components):
    session_id = components.next()
    if session_id not in sessions:
        return "f-2-Session_identifier_`" + str(session_id) + "`_is_unknown."
    session = sessions[session_id]
    packets = map(base64.b64decode, components)
    LOG("Forwarding " + str(len(packets)) + " packets for session " + str(session_id))
    for packet in packets:
        # TODO: This only takes care of the last error?
        err = session.forward(packet)
    if err == NO_ERROR:
        return "s"
    elif err == INVALID_PACKET:
        LOG("Failed to forward invalid packet for session " + str(session_id))
        return "f-0-Packet_is_Invalid"
    elif err == NO_FREE_PORT:
        LOG("Could not find a free port to forward packet for session " + str(session_id))
        return "f-0-Could_not_find_a_free_port"

def got_request_packets(components):
    session_id = components.next()
    if session_id not in sessions:
        return "f-2-Session_identifier_`" + str(session_id) + "`_is_unknown."
    session = sessions[session_id]
    data = session.request()
    LOG("Session " + str(session_id) + " requested packets, replying with " + str(len(data)) + " packets in " + str(sizeof_list(data)) + " bytes.")
    response = "s"
    for packet in data:
        response += "-" + packet
    return response

def got_end_session(components):
    session_id = components.next()
    if session_id not in sessions:
        return "f-2-Session_identifier_`" + str(session_id) + "`_is_unknown."
    session = sessions[session_id]
    LOG("Ending session: " + str(session_id))
    del sessions[session_id]
    return "s"
