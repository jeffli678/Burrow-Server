from __future__ import print_function

import copy
import json
import uuid
import collections
import re
import sys
import multiprocessing

from dnslib import RR,RCODE
from dnslib.label import DNSLabel
from dnslib.server import DNSServer, DNSHandler, BaseResolver, DNSLogger
from expiringdict import ExpiringDict

import session
from burrow_logging import burrow_log
def LOG(s):
    burrow_log(s, 4)

# This function parses incoming DNS requests as per the Transmission
# API format documented in README.md.
# The Begin, Continue, and End API endpoints are the only endpoints
# recognized. Requests that are properly formatted but refer to
# unknown API endpoints are categorized as Other.
# Incorrectly formatted requests are categorized as Failure.
Begin = collections.namedtuple('Begin', 'prefix')
Continue = collections.namedtuple('Continue', 'data index id')
End = collections.namedtuple('End', 'length id')
Other = collections.namedtuple('Other', 'host')
Failure = collections.namedtuple('Failure', 'host')
def parse_url(url):
    try:
        copy = url
        assert(isinstance(url, DNSLabel))
        assert(url.matchSuffix("burrow.tech"))
        url = url.stripSuffix("burrow.tech")
        if url.matchSuffix("begin"):
            url = url.stripSuffix("begin")
            if len(url.label) < 1:
                raise ValueError
            return Begin(url.label[-1])
        elif url.matchSuffix("continue"):
            url = url.stripSuffix("continue")
            if len(url.label) < 3:
                raise ValueError
            data = "".join(url.label[:-2]).replace(".", "")
            return Continue(data, int(url.label[-2]), url.label[-1])
        elif url.matchSuffix("end"):
            url = url.stripSuffix("end")
            if len(url.label) < 2:
                raise ValueError
            return End(int(url.label[-2]), url.label[-1])
        else:
            return Other(copy)
    except ValueError:
            return Failure(copy)


def dict_to_attributes(d):
    # Implement the standard way of representing attributes
    # in TXT records, see RFC 1464
    # Essentially turns {a: b, c: d} into ["a=b","c=d"]
    output = []
    for (key, value) in d.iteritems():
        output.append(str(key) + "=" + str(value))
    output.append("$count=" + str(len(d)))
    return output

# These two functions are a somewhat clunky way of getting DNS
# information into dnslib by turning it into text in the Zone File
# format, which can then be easily parsed by dnslib.
# TODO (low-pri): this should be eliminated and we should switch
# to directly getting DNS information into dnslib using the built-in functions.
def generate_TXT_zone_line(host, text):
    assert(host.endswith(".burrow.tech."))
    # Split the text into 250-char substrings if necessary
    split_text = [text[i:i+250] for i in range(0, len(text), 250)]
    prepared_text = '"' + '" "'.join(split_text) + '"\n'
    zone = host + " 60 IN TXT " + prepared_text
    return zone
def generate_TXT_zone(host, text_list):
    output = ""
    for t in text_list:
       output += generate_TXT_zone_line(host, t)
    return output

def is_domain_safe(s):
    domain_safe_matcher = re.compile(r'[A-Za-z0-9-+/]').search
    return bool(domain_safe_matcher(s))

class Transmission:
    """
        Represents an incoming transmission from a client.
    """
    def __init__(self, id):
        self.id = id
        self.data = {} # {index: data}
        self.final_contents = ""  # this is filled in by the end() method

    def add_data(self, data, index):
        # Indices can arrive out of order
        # Ignore data from indices we've already seen
        if (index not in self.data):
            self.data[index] = data

    def end(self, length):
        if all (k in self.data for k in range(length)):
            for i in range(length):
                self.final_contents += self.data[i]
            return True
        else:
            return False

    def __repr__(self):
        return "<Transmission " + self.id + ", " + str(self.data) + ">"

class BurrowResolver(BaseResolver):
    """
        Respond with fixed response to some requests, and treat others as Transmission layer traffic.
    """
    def __init__(self):
        fixed_zone = open("fixed_zone/primary.txt").read() + open("fixed_zone/tests.txt").read()
        self.fixedrrs = RR.fromZone(fixed_zone)
        self.active_transmissions = {}  # Dictionary of Transmission objects.
                                        # Their ID's are the keys, for easy/quick lookup.
        self.cache = ExpiringDict(max_len=100000, max_age_seconds=70)
        self.transmission_handler_lock = multiprocessing.Lock()

    def resolve(self,request,handler):
        reply = request.reply()  # the object that this function will return in the end, with modifications
        qname = request.q.qname  # the domain that was looked up

        burrow_log("Request for " + str(DNSLabel(qname.label[-5:])), 0)

        # First, we make sure the domain ends in burrow.tech.
        # If not, we return an NXDOMAIN result.
        if not qname.matchSuffix("burrow.tech"):
            reply.header.rcode = RCODE.NXDOMAIN
            return reply
       
        # Next, we try to look up the domain in our list of fixed test records.
        found_fixed_rr = False
        for rr in self.fixedrrs:
            a = copy.copy(rr)
            if (a.rname == qname):
                found_fixed_rr = True
                LOG("Found a fixed record for " + str(a.rname))
                reply.add_answer(a)
        if found_fixed_rr:
            return reply

        # Alright, if we've gotten here it must be a Transmission API message!
        assert(not found_fixed_rr)
        response_dict = self.handle_transmission_api_message(qname)
        zone = generate_TXT_zone(str(qname), dict_to_attributes(response_dict))
        rrs = RR.fromZone(zone)
        for rr in rrs:
            reply.add_answer(rr)
        return reply

    def handle_transmission_api_message(self, qname):
        self.transmission_handler_lock.acquire()
        # If we recently responded to this lookup, we don't want to re-handle it.
        if qname in self.cache:
            LOG("Cache hit!")
            response_dict = self.cache[qname]
            self.transmission_handler_lock.release()
            return response_dict

        # Otherwise, handle as a new lookup.
        parsed = parse_url(qname)

        if isinstance(parsed, Failure):
            response_dict = {'success': False, 'error': "You used the API incorrectly."}

        elif isinstance(parsed, Other):
            response_dict = {'success': False, 'error': "This is not an API endpoint"}

        elif isinstance(parsed, Begin):
            transmission_id = uuid.uuid4().hex[-8:]
            self.active_transmissions[transmission_id] = Transmission(transmission_id)
            LOG("Began transmission with id: " + str(transmission_id))
            response_dict = {'success': True, 'transmission_id': transmission_id}

        elif isinstance(parsed, Continue):
            try:
                self.active_transmissions[parsed.id].add_data(parsed.data, parsed.index)
                LOG("Continuing transmission " + str(parsed.id))
                response_dict = {'success': True}
            except KeyError:
                LOG("Error: tried to continue a transmission that doesn't exist: " + str(parsed.id))
                response_dict = {'success': False, 'error': "Tried to continue a transmission that doesn't exist."}

        elif isinstance(parsed, End):
            transmission = self.active_transmissions.get(parsed.id)
            response_dict = None
            if transmission is not None:
                del self.active_transmissions[parsed.id]
                if transmission.end(parsed.length):
                    final_contents = transmission.final_contents
                    LOG("Ending transmission " + str(parsed.id) +
                        ". Final contents: " + ((final_contents[:15] + '...') if len(final_contents) > 15 else final_contents))
                    response = session.handle_message(final_contents)
                    assert(is_domain_safe(response))
                    response_dict = {'success': True, 'contents': response}
                else:
                    response_dict = {'success': False, 'error': ".end called with length that didn't match number of .continue's received."}
            else:
                LOG("Error: tried to end a transmission that doesn't exist: " + str(parsed.id))
                response_dict = {'success': False, 'error': "Tried to end a transmission that doesn't exist."}

        # Cache the response in case of duplicate lookups
        self.cache[qname] = response_dict
        self.transmission_handler_lock.release()
        return response_dict



if __name__ == '__main__':

    import argparse,sys,time

    p = argparse.ArgumentParser(description="Burrow DNS Resolver")
    p.add_argument("--port","-p",type=int,default=53,
                    metavar="<port>",
                    help="Server port (default:53)")
    p.add_argument("--address","-a",default="",
                    metavar="<address>",
                    help="Listen address (default:all)")
    p.add_argument("--udplen","-u",type=int,default=0,
                    metavar="<udplen>",
                    help="Max UDP packet length (default:0)")
    p.add_argument("--notcp",action='store_true',default=False,
                    help="UDP server only (default: UDP and TCP)")
    p.add_argument("--log",default="truncated,error",
                    help="Log hooks to enable (default: -request,-reply,+truncated,+error,-recv,-send,-data)")
    p.add_argument("--log-prefix",action='store_true',default=False,
                    help="Log prefix (timestamp/handler/resolver) (default: False)")
    args = p.parse_args()

    resolver = BurrowResolver()
    logger = DNSLogger(args.log,args.log_prefix)

    burrow_log("", 0)
    burrow_log("Starting Burrow Resolver (%s:%d) [%s]" % (
                        args.address or "*",
                        args.port,
                        "UDP" if args.notcp else "UDP/TCP"), 0)

    if args.udplen:
        DNSHandler.udplen = args.udplen

    udp_server = DNSServer(resolver,
                           port=args.port,
                           address=args.address,
                           logger=logger)
    udp_server.start_thread()

    if not args.notcp:
        tcp_server = DNSServer(resolver,
                               port=args.port,
                               address=args.address,
                               tcp=True,
                               logger=logger)
        tcp_server.start_thread()

    while udp_server.isAlive():
        time.sleep(1)

