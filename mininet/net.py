#!/usr/bin/python

"""
Mininet: A simple networking testbed for OpenFlow!

Mininet creates scalable OpenFlow test networks by using
process-based virtualization and network namespaces. 

Simulated hosts are created as processes in separate network
namespaces. This allows a complete OpenFlow network to be simulated on
top of a single Linux kernel.

Each host has:
   A virtual console (pipes to a shell)
   A virtual interfaces (half of a veth pair)
   A parent shell (and possibly some child processes) in a namespace
   
Hosts have a network interface which is configured via ifconfig/ip
link/etc. with data network IP addresses (e.g. 192.168.123.2 )

This version supports both the kernel and user space datapaths
from the OpenFlow reference implementation.

In kernel datapath mode, the controller and switches are simply
processes in the root namespace.

Kernel OpenFlow datapaths are instantiated using dpctl(8), and are
attached to the one side of a veth pair; the other side resides in the
host namespace. In this mode, switch processes can simply connect to the
controller via the loopback interface.

In user datapath mode, the controller and switches are full-service
nodes that live in their own network namespaces and have management
interfaces and IP addresses on a control network (e.g. 10.0.123.1,
currently routed although it could be bridged.)

In addition to a management interface, user mode switches also have
several switch interfaces, halves of veth pairs whose other halves
reside in the host nodes that the switches are connected to.

Naming:
   Host nodes are named h1-hN
   Switch nodes are named s0-sN
   Interfaces are named {nodename}-eth0 .. {nodename}-ethN,

Thoughts/TBD:

   It should be straightforward to add a function to read
   OpenFlowVMS spec files, but I haven't done so yet.
   For the moment, specifying configurations and tests in Python
   is straightforward and relatively concise.
   Soon, we may want to split the various subsystems (core,
   topology/network, cli, tests, etc.) into multiple modules.
   Currently nox support is in nox.py.
   We'd like to support OpenVSwitch as well as the reference
   implementation.
   
Bob Lantz
rlantz@cs.stanford.edu

History:
11/19/09 Initial revision (user datapath only)
11/19/09 Mininet demo at OpenFlow SWAI meeting
12/08/09 Kernel datapath support complete
12/09/09 Moved controller and switch routines into classes
12/12/09 Added subdivided network driver workflow
12/13/09 Added support for custom controller and switch classes
"""
import os
import re
from subprocess import call
import sys
from time import sleep
#flush = sys.stdout.flush
#import os, re, signal, sys, select

from mininet.logging_mod import lg, set_loglevel
from mininet.node import Host, Controller, Switch, ControllerParams
from mininet.topo import TreeTopo
from mininet.util import quietRun, fixLimits
from mininet.util import make_veth_pair, move_intf, retry, MOVEINTF_DELAY

DATAPATHS = ['kernel'] #['user', 'kernel']


def init():
    "Initialize Mininet."
    if os.getuid() != 0:
        # Note: this script must be run as root
        # Perhaps we should do so automatically!
        print "*** Mininet must run as root."
        exit(1)
    # If which produces no output, then netns is not in the path.
    # May want to loosen this to handle netns in the current dir.
    if not quietRun(['which', 'netns']):
        raise Exception("Could not find netns; see INSTALL")
    fixLimits()


class Mininet(object):
    '''Network emulation with hosts spawned in network namespaces.'''

    def __init__(self, topo, switch, host, controller, cparams,
                 build = True, xterms = False, cleanup = False,
                 in_namespace = False, switch_is_kernel = True):
        '''Create Mininet object.

        @param topo Topo object
        @param switch Switch class
        @param host Host class
        @param controller Controller class
        @param cparams ControllerParams object
        @param now build now?
        @param xterms if build now, spawn xterms?
        @param cleanup if build now, cleanup before creating?
        @param in_namespace spawn switches and hosts in their own namespace?
        '''
        self.topo = topo
        self.switch = switch
        self.host = host
        self.controller = controller
        self.cparams = cparams
        self.nodes = {} # dpid to Node{Host, Switch} objects
        self.controllers = {} # controller name to Controller objects
        self.dps = 0 # number of created kernel datapaths
        self.in_namespace = in_namespace
        self.switch_is_kernel = switch_is_kernel

        self.kernel = True #temporary!

        if build:
            self.build(xterms, cleanup)

    def _add_host(self, dpid):
        '''Add host.

        @param dpid DPID of host to add
        '''
        host = self.host('h_' + self.topo.name(dpid))
        # for now, assume one interface per host.
        host.intfs.append('h_' + self.topo.name(dpid) + '-eth0')
        self.nodes[dpid] = host
        #lg.info('%s ' % host.name)

    def _add_switch(self, dpid):
        '''
        @param dpid DPID of switch to add
        '''
        sw = None
        if self.switch_is_kernel:
            sw = self.switch('s_' + self.topo.name(dpid), 'nl:' + str(self.dps))
            self.dps += 1
        else:
            sw = self.switch('s_' + self.topo.name(dpid))
        self.nodes[dpid] = sw

    def _add_link(self, src, dst):
        '''Add link.

        @param src source DPID
        @param dst destination DPID
        '''
        src_port, dst_port = self.topo.port(src, dst)
        src_node = self.nodes[src]
        dst_node = self.nodes[dst]
        src_intf = src_node.intfName(src_port)
        dst_intf = dst_node.intfName(dst_port)
        make_veth_pair(src_intf, dst_intf)
        src_node.intfs.append(src_intf)
        dst_node.intfs.append(dst_intf)
        #lg.info('\n')
        #lg.info('added intf %s to src node %x\n' % (src_intf, src))
        #lg.info('added intf %s to dst node %x\n' % (dst_intf, dst))
        if src_node.inNamespace:
            #lg.info('moving src w/inNamespace set\n')
            retry(3, MOVEINTF_DELAY, move_intf, src_intf, src_node)
        if dst_node.inNamespace:
            #lg.info('moving dst w/inNamespace set\n')
            retry(3, MOVEINTF_DELAY, move_intf, dst_intf, dst_node)
        src_node.connection[src_intf] = (dst_node, dst_intf)
        dst_node.connection[dst_intf] = (src_node, src_intf)

    def _add_controller(self, controller):
        '''Add controller.

        @param controller Controller class
        '''
        controller = self.controller('c0', kernel = self.kernel)
        self.controllers['c0'] = controller

    # Control network support:
    #
    # Create an explicit control network. Currently this is only
    # used by the user datapath configuration.
    #
    # Notes:
    #
    # 1. If the controller and switches are in the same (e.g. root)
    #    namespace, they can just use the loopback connection.
    #    We may wish to do this for the user datapath as well as the
    #    kernel datapath.
    #
    # 2. If we can get unix domain sockets to work, we can use them
    #    instead of an explicit control network.
    #
    # 3. Instead of routing, we could bridge or use 'in-band' control.
    #
    # 4. Even if we dispense with this in general, it could still be
    #    useful for people who wish to simulate a separate control
    #    network (since real networks may need one!)

    def _configureControlNetwork(self):
        '''Configure control network.'''
        self._configureRoutedControlNetwork()

    def _configureRoutedControlNetwork(self):
        '''Configure a routed control network on controller and switches.

        For use with the user datapath only right now.

        @todo(brandonh) Test this code and verify that user-space works!
        '''
        # params were: controller, switches, ips

        controller = self.controllers['c0']
        lg.info('%s <-> ' % controller.name)
        for switch_dpid in self.topo.switches():
            switch = self.nodes[switch_dpid]
            lg.info('%s ' % switch.name)
            sip = self.topo.ip(switch_dpid)#ips.next()
            sintf = switch.intfs[0]
            node, cintf = switch.connection[sintf]
            if node != controller:
                lg.error('*** Error: switch %s not connected to correct'
                         'controller' %
                         switch.name)
                exit(1)
            controller.setIP(cintf, self.cparams.ip, '/' +
                             self.cparams.subnet_size)
            switch.setIP(sintf, sip, '/' + self.cparams.subnet_size)
            controller.setHostRoute(sip, cintf)
            switch.setHostRoute(self.cparams.ip, sintf)
        lg.info('\n')
        lg.info('*** Testing control network\n')
        while not controller.intfIsUp(controller.intfs[0]):
            lg.info('*** Waiting for %s to come up\n', controller.intfs[0])
            sleep(1)
        for switch_dpid in self.topo.switches():
            switch = self.nodes[switch_dpid]
            while not switch.intfIsUp(switch.intfs[0]):
                lg.info('*** Waiting for %s to come up\n' % switch.intfs[0])
                sleep(1)
            if self.ping_test(hosts=[switch, controller]) != 0:
                lg.error('*** Error: control network test failed\n')
                exit(1)
        lg.info('\n')

    def _config_hosts( self ):
        '''Configure a set of hosts.'''
        # params were: hosts, ips
        for host_dpid in self.topo.hosts():
            host = self.nodes[host_dpid]
            hintf = host.intfs[0]
            host.setIP(hintf, self.topo.ip(host_dpid),
                       '/' + str(self.cparams.subnet_size))
            host.setDefaultRoute(hintf)
            # You're low priority, dude!
            quietRun('renice +18 -p ' + repr(host.pid))
            lg.info('%s ', host.name)
        lg.info('\n')

    def build(self, xterms, cleanup):
        '''Build mininet.

        At the end of this function, everything should be connected and up.

        @param xterms spawn xterms on build?
        @param cleanup cleanup before creating?
        '''
        if cleanup:
            pass # cleanup
        # validate topo?
        kernel = self.kernel
        if kernel:
            lg.info('*** Using kernel datapath\n')
        else:
            lg.info('*** Using user datapath\n')
        lg.info('*** Adding controller\n')
        self._add_controller(self.controller)
        lg.info('*** Creating network\n')
        lg.info('*** Adding hosts:\n')
        for host in sorted(self.topo.hosts()):
            self._add_host(host)
            lg.info('0x%x ' % host)
        lg.info('\n*** Adding switches:\n')
        for switch in sorted(self.topo.switches()):
            self._add_switch(switch)
            lg.info('0x%x ' % switch)
        lg.info('\n*** Adding edges: ')
        for src, dst in sorted(self.topo.edges()):
            self._add_link(src, dst)
            lg.info('(0x%x, 0x%x) ' % (src, dst))
        lg.info('\n')

        if not kernel:
            lg.info('*** Configuring control network\n')
            self._configureControlNetwork()

        lg.info('*** Configuring hosts\n')
        self._config_hosts()

        if xterms:
            pass # build xterms

    def start(self):
        '''Start controller and switches\n'''
        lg.info('*** Starting controller\n')
        self.controllers['c0'].start()
        #for controller in self.controllers:
        #    controller.start()
        lg.info('*** Starting %s switches\n' % len(self.topo.switches()))
        for switch_dpid in self.topo.switches():
            switch = self.nodes[switch_dpid]
            #lg.info('switch = %s' % switch)
            lg.info('0x%x ' % switch_dpid)
            switch.start(self.controllers['c0'])
        lg.info('\n')

    def stop(self):
        '''Stop the controller(s), switches and hosts\n'''
        lg.info('*** Stopping %i hosts\n' % len(self.topo.hosts()))
        for host_dpid in self.topo.hosts():
            host = self.nodes[host_dpid]
            lg.info('%s ' % host.name)
            host.terminate()
        lg.info('\n')
        lg.info('*** Stopping %i switches\n' % len(self.topo.switches()))
        for switch_dpid in self.topo.switches():
            switch = self.nodes[switch_dpid]
            lg.info('%s' % switch.name)
            switch.stop()
        lg.info('\n')
        lg.info('*** Stopping controller\n')
        #for controller in self.controllers.iteriterms():
        self.controllers['c0'].stop()
        lg.info('*** Test complete\n')

    def run(self, test, **params):
        '''Perform a complete start/test/stop cycle.'''
        self.start()
        lg.info('*** Running test\n')
        result = test(self, **params)
        self.stop()
        return result

    @staticmethod
    def _parse_ping(pingOutput):
        '''Parse ping output and return packets sent, received.'''
        r = r'(\d+) packets transmitted, (\d+) received'
        m = re.search( r, pingOutput )
        if m == None:
            lg.error('*** Error: could not parse ping output: %s\n' %
                     pingOutput)
            exit(1)
        sent, received = int(m.group(1)), int(m.group(2))
        return sent, received

    def ping_test(self, hosts = None, verbose = False):
        '''Ping between all specified hosts.

        @param hosts list of host DPIDs
        @param verbose verbose printing
        @return ploss packet loss percentage
        '''
        #self.start()
        # check if running - only then, start?
        packets = 0
        lost = 0
        ploss = None
        if not hosts:
            hosts = self.topo.hosts()
        for node_dpid in hosts:
            node = self.nodes[node_dpid]
            if verbose:
                lg.info('%s -> ' % node.name)
            for dest_dpid in hosts:
                dest = self.nodes[dest_dpid]
                if node != dest:
                    result = node.cmd('ping -c1 ' + dest.IP())
                    sent, received = self._parse_ping(result)
                    packets += sent
                    if received > sent:
                        lg.error('*** Error: received too many packets')
                        lg.error('%s' % result)
                        node.cmdPrint('route')
                        exit( 1 )
                    lost += sent - received
                    if verbose:
                        lg.info(('%s ' % dest.name) if received else 'X ')
            if verbose:
                lg.info('\n')
            ploss = 100 * lost / packets
        if verbose:
            lg.info('%d%% packet loss (%d/%d lost)\n' % (ploss, lost, packets))
            #flush()
        #self.stop()
        return ploss

    def interact(self):
        '''Start network and run our simple CLI.'''
        self.run(MininetCLI)


class MininetCLI(object):
    '''Simple command-line interface to talk to nodes.'''
    cmds = ['?', 'help', 'nodes', 'net', 'sh', 'ping_all', 'exit', \
            'ping_pair'] #'iperf'

    def __init__(self, mininet):
        self.mn = mininet
        self.nodemap = {} # map names to Node objects
        for node in self.mn.nodes.values():
            self.nodemap[node.name] = node
        self.nodemap['c0'] = self.mn.controllers['c0']
        self.nodelist = self.nodemap.values()
        self.run()

    # Commands
    def help(self, args):
        '''Semi-useful help for CLI.'''
        help_str = 'Available commands are:' + str(self.cmds) + '\n' + \
                   'You may also send a command to a node using:\n' + \
                   '  <node> command {args}\n' + \
                   'For example:\n' + \
                   '  mininet> h0 ifconfig\n' + \
                   '\n' + \
                   'The interpreter automatically substitutes IP ' + \
                   'addresses\n' + \
                   'for node names, so commands like\n' + \
                   '  mininet> h0 ping -c1 h1\n' + \
                   'should work.\n' + \
                   '\n\n' + \
                   'Interactive commands are not really supported yet,\n' + \
                   'so please limit commands to ones that do not\n' + \
                   'require user interaction and will terminate\n' + \
                   'after a reasonable amount of time.\n'
        print(help_str)

    def nodes(self, args):
        '''List all nodes.'''
        lg.info('available nodes are: \n%s\n',
                ' '.join([node.name for node in sorted(self.nodelist)]))

    def net(self, args):
        '''List network connection.'''
        for switch_dpid in self.mn.topo.switches():
            switch = self.mn.nodes[switch_dpid]
            lg.info('%s <->', switch.name)
            for intf in switch.intfs:
                node, remoteIntf = switch.connection[intf]
                lg.info(' %s' % node.name)
            lg.info('\n')

    def sh(self, args):
        '''Run an external shell command'''
        call( [ 'sh', '-c' ] + args )

    def ping_all(self, args):
        '''Ping between all hosts.'''
        self.mn.ping_test(verbose = True)

    def ping_pair(self, args):
        '''Ping between first two hosts, useful for testing.'''
        hosts_unsorted = sorted(self.mn.topo.hosts())
        hosts = [hosts_unsorted[0], hosts_unsorted[1]]
        self.mn.ping_test(hosts = hosts, verbose = True)

    def run(self):
        '''Read and execute commands.'''
        lg.warn('*** Starting CLI:\n')
        while True:
            lg.warn('mininet> ')
            input = sys.stdin.readline()
            if input == '':
                break
            if input[-1] == '\n':
                input = input[:-1]
            cmd = input.split(' ')
            first = cmd[0]
            rest = cmd[1:]
            if first in self.cmds and hasattr(self, first):
                getattr(self, first)(rest)
            elif first in self.nodemap and rest != []:
                node = self.nodemap[first]
                # Substitute IP addresses for node names in command
                rest = [self.nodemap[arg].IP() if arg in self.nodemap else arg
                        for arg in rest]
                rest = ' '.join(rest)
                # Interactive commands don't work yet, and
                # there are still issues with control-c
                lg.warn('*** %s: running %s\n' % (node.name, rest))
                node.sendCmd(rest)
                while True:
                    try:
                        done, data = node.monitor()
                        lg.info('%s\n' % data)
                        if done:
                            break
                    except KeyboardInterrupt:
                        node.sendInt()
            elif first == '':
                pass
            elif first in ['exit', 'quit']:
                break
            elif first == '?':
                self.help( rest )
            else:
                lg.error('CLI: unknown node or command: < %s >\n' % first)
            #lg.info('*** CLI: command complete\n')
        return 'exited by user command'


if __name__ == '__main__':
    if len(sys.argv) > 1:
        set_loglevel(sys.argv[1])
    else:
        set_loglevel('info')

    init()
    results = {}
    lg.info("*** Welcome to Mininet!\n")
    lg.info("*** Look in examples/ for more examples\n\n")
    lg.info("*** Testing Mininet with kernel and user datapath\n")
    for datapath in DATAPATHS:
        k = datapath == 'kernel'
        controller_params = ControllerParams(0x0a000000, 8) # 10.0.0.0/8
        mn = Mininet(TreeTopo(), Switch, Host, Controller,
                         controller_params)
        mn.start()
        dropped = mn.ping_test()
        results[datapath] = "%i%% dropped" % dropped
        mn.stop()

    lg.info("*** Test results: %s\n", results)