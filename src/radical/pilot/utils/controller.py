

import os
import sys
import copy
import time
import pprint
import signal

import threading       as mt
import multiprocessing as mp
import radical.utils   as ru

from ..          import constants      as rpc
from ..          import states         as rps

from .misc       import hostip
from .prof_utils import timestamp      as rpu_timestamp

from .queue      import Queue          as rpu_Queue
from .queue      import QUEUE_OUTPUT   as rpu_QUEUE_OUTPUT
from .queue      import QUEUE_INPUT    as rpu_QUEUE_INPUT
from .queue      import QUEUE_BRIDGE   as rpu_QUEUE_BRIDGE

from .pubsub     import Pubsub         as rpu_Pubsub
from .pubsub     import PUBSUB_PUB     as rpu_PUBSUB_PUB
from .pubsub     import PUBSUB_SUB     as rpu_PUBSUB_SUB
from .pubsub     import PUBSUB_BRIDGE  as rpu_PUBSUB_BRIDGE


# ==============================================================================
#
class Controller(object):
    """
    A Controller is an entity which creates, manages and destroys Bridges and
    Components, according to some configuration.
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):
        """
        The given configuration is inspected for a 'bridges' and a 'components'
        entry.  Under bridges, we expect either a dictionary of the form:

           {
             'bridge_name_a' : {},
             'bridge_name_b' : {},
             'bridge_name_c' : {'in'  : 'address',
                                'out' : 'address'},
             'bridge_name_d' : {'in'  : 'address',
                                'out' : 'address'},
             ...
           }

        where bridges with unspecified addresses will be started by the
        controller, on initialization.  The controller will need at least
        a 'control_pubsub' bridge, either as address or to start, for component
        coordination (see below).

        The components field is expected to have entries of the form:

           {
             'component_a' : 1,
             'component_b' : 3
             ...
           }

        where the respective number of component instances will be created.
        After creation, the controller will issue heartbeat signals on the same
        pubsub, in the expectation that the components will self-destruct if
        they miss that heartbeat, ie. in the case of unexpected controller
        failer or unclean shutdown.

        Components and Bridges will get passed a copy of the config on creation.
        Components will also get passed a copy of the bridge addresses dict.

        The controller has one additional method, `stop()`, which will destroy
        all components and bridges (in this order).  Stop is not called on
        `__del__()`, and thus must be invoked explicitly.
        """

        # this method can only be called from the main thread
        self_thread = mt.current_thread()
        assert(self_thread.name == 'MainThread')
        
        assert(cfg['owner']), 'controller config always needs an "owner"'

        self._session = session

        # we use a uid to uniquely identify message to and from bridges and
        # components, and for logging/profiling.
        self._uid   = '%s.ctrl' % cfg['owner']
        self._owner = cfg['owner']

        # get debugging, logging, profiling set up
        self._debug = cfg.get('debug')
        self._dh    = ru.DebugHelper(name=self.uid)
        self._log   = self._session._get_logger(self._owner, level=self._debug)

        # we keep a copy of the cfg around, so that we can pass it on when
        # creating components.
        self._cfg = copy.deepcopy(cfg)

        # Dig any releavnt information from the cfg.  That most importantly
        # contains bridge addresses etc, but also the heartbeat settings.
        self._ctrl_cfg = {
                'bridges'            : copy.deepcopy(cfg.get('bridges')),
                'heart'              : cfg.get('heart'),
                'heartbeat_interval' : cfg.get('heartbeat_interval'),
                'heartbeat_timeout'  : cfg.get('heartbeat_timeout'),
        }
        # we also ceep the component information around, in case we need to
        # start any
        self._comp_cfg = copy.deepcopy(cfg.get('components', {}))

        self._log.info('initialize %s', self.uid)

        # keep handles to bridges and components started by us, but also to
        # other things handed to us via 'add_watchables()' (such as sub-agents)
        self._bridges_to_watch    = list()
        self._components_to_watch = list()

        # control thread activities
        self._term = mt.Event()

        # set up for eventual heartbeat sending/receiving
        self._heartbeat_interval = self._ctrl_cfg.get('heartbeat_interval',  10)
        self._heartbeat_timeout  = self._ctrl_cfg.get('heartbeat_timeout', 3*10)
        self._heartbeat_thread   = None    # heartbeat thread
        self._heartbeat_tname    = None    # thread name
        self._heartbeat_term     = None    # thread termination signal


        # set up for eventual component/brodge watching
        self._watcher_thread     = None    # watcher thread
        self._watcher_tname      = None    # thread name
        self._watcher_term       = None    # thread termination signal


        # We keep a static typemap for component startup. If we ever want to
        # become reeeealy fancy, we can derive that typemap from rp module
        # inspection.
        #
        # NOTE:  I'd rather have this as class data than as instance data, but
        #        python stumbles over circular imports at that point :/

        from .. import worker as rpw
        from .. import pmgr   as rppm
        from .. import umgr   as rpum
        from .. import agent  as rpa

        self._typemap = {rpc.UPDATE_WORKER                  : rpw.Update,

                         rpc.PMGR_LAUNCHING_COMPONENT       : rppm.Launching,

                         rpc.UMGR_STAGING_INPUT_COMPONENT   : rpum.Input,
                         rpc.UMGR_SCHEDULING_COMPONENT      : rpum.Scheduler,
                         rpc.UMGR_STAGING_OUTPUT_COMPONENT  : rpum.Output,

                         rpc.AGENT_STAGING_INPUT_COMPONENT  : rpa.Input,
                         rpc.AGENT_SCHEDULING_COMPONENT     : rpa.Scheduler,
                         rpc.AGENT_EXECUTING_COMPONENT      : rpa.Executing,
                         rpc.AGENT_STAGING_OUTPUT_COMPONENT : rpa.Output
                         }

        # complete the setup with bridge and component creation
        self._start_bridges()
        self._start_components()


    # --------------------------------------------------------------------------
    #
    @property
    def uid(self):
        return self._uid


    # --------------------------------------------------------------------------
    #
    @property
    def ctrl_cfg(self):

        return copy.deepcopy(self._ctrl_cfg)


    # --------------------------------------------------------------------------
    #
    def stop(self):

        # this method can only be called from the main thread
        self_thread = mt.current_thread()
        assert(self_thread.name == 'MainThread')

        # let all threads know whats up...
        self._term.set()

        self._log.debug('%s stopped', self.uid)

        if self._heartbeat_thread:
            self._log.debug('%s stop    hbeat', self.uid)
            self._heartbeat_term.set()
            self._log.debug('%s stopped hbeat', self.uid)
            self._log.debug('%s join    hbeat', self.uid)
            self._heartbeat_thread.join()
            self._log.debug('%s joined  hbeat', self.uid)

        if self._watcher_thread:
            self._log.debug('%s stop    watch', self.uid)
            self._watcher_term.set()
            self._log.debug('%s stopped watch', self.uid)
            self._log.debug('%s join    watch', self.uid)
            self._watcher_thread.join()
            self._log.debug('%s joined  watch', self.uid)

        # we first stop all components (and sub-agents), and only then tear down
        # the communication bridges.  That way, the bridges will be available
        # during shutdown.

        for to_stop_list in [self._components_to_watch, self._bridges_to_watch]:

            for t in to_stop_list:
                self._log.debug('%s stop    %s', self.uid, t.name)
                t.stop()
                self._log.debug('%s stopped %s', self.uid, t.name)

            for t in to_stop_list:
                self._log.debug('%s join    %s', self.uid, t.name)
                t.join()
                self._log.debug('%s joined  %s', self.uid, t.name)

        self._log.debug('%s stopped', self.uid)


    # --------------------------------------------------------------------------
    #
    def _start_bridges(self):
        """
        Helper method to start a given list of bridges.  The type of bridge
        (queue or pubsub) is derived from the name.  The bridge addresses are
        kept in self._ctrl_cfg.

        If new components are later started via self._start_components, then the
        address map is passed on as part of the component config,
        """

        # this method can only be called from the main thread
        self_thread = mt.current_thread()
        assert(self_thread.name == 'MainThread')
        
        # we *always* need bridges defined in the config, at least the should be
        # the addresses for the control and log bridges (or we start them)
        assert(self._ctrl_cfg['bridges'])
        assert(self._ctrl_cfg['bridges'][rpc.LOG_PUBSUB])
        assert(self._ctrl_cfg['bridges'][rpc.CONTROL_PUBSUB])

        # the control channel is special: whoever creates the control channel
        # will also send heartbeats on it, for all components which use it.
        # Thus, if we will create the control channel, we become the heart --
        # otherwise we expect a heart UID set in the config.
        if self._ctrl_cfg['bridges'][rpc.CONTROL_PUBSUB].get('addr_in'):
            # control bridge address is defined -- heart should be known
            assert(self._ctrl_cfg['heart']), 'control bridge w/o heartbeat src?'
        else:
            # we will have to start the bridge, and become the heart.
            self._ctrl_cfg['heart'] = self._owner

        # start all bridges which don't yet have an address
        bridges = list()
        for bname,bcfg in self._ctrl_cfg['bridges'].iteritems():

            addr_in  = bcfg.get('addr_in')
            addr_out = bcfg.get('addr_out')

            if addr_in:
                # bridge is running
                assert(addr_out)

            else:
                # bridge needs starting
                self._log.info('create bridge %s', bname)
            
                if bname.endswith('queue'):
                    bridge = rpu_Queue(self._session, bname, rpu_QUEUE_BRIDGE, bcfg)
                elif bname.endswith('pubsub'):
                    bridge = rpu_Pubsub(self._session, bname, rpu_PUBSUB_BRIDGE, bcfg)
                else:
                    raise ValueError('unknown bridge type for %s' % bname)

                # FIXME: check if bridge is up and running
                # we keep a handle to the bridge for later shutdown
                bridges.append(bridge)

                addr_in  = ru.Url(bridge.bridge_in)
                addr_out = ru.Url(bridge.bridge_out)

                # we just started the bridge -- use the local hostip for 
                # the address!
                # FIXME: this should be done in the bridge already
                addr_in.host  = hostip()
                addr_out.host = hostip()

                self._ctrl_cfg['bridges'][bname]['addr_in']  = str(addr_in)
                self._ctrl_cfg['bridges'][bname]['addr_out'] = str(addr_out)

                self._log.info('created bridge %s (%s)', bname, bridge.name)

        if bridges:
            # some bridges are alive -- we can start monitoring them.  
            # We may have done so before, so check
            if not self._watcher_thread:
                self._watcher_term   = mt.Event()
                self._watcher_tname  = '%s.watcher' % self._uid
                self._watcher_thread = mt.Thread(target=self._watcher,
                                                 args=[self._watcher_term],
                                                 name=self._watcher_tname)
                self._watcher_thread.start()

        # make sure the bridges are watched:
        self._bridges_to_watch += bridges

        # if we are the root of a component tree, start sending heartbeats 
        self._log.debug('send heartbeat?: %s =? %s', self._owner, self._ctrl_cfg['heart'])
      # print 'send heartbeat?: %s =? %s' % (self._owner, self._ctrl_cfg['heart'])
        if self._owner == self._ctrl_cfg['heart']:

            if not self._heartbeat_thread:

                # we need to issue heartbeats!
                self._heartbeat_term   = mt.Event()
                self._heartbeat_tname  = '%s.heartbeat' % self._uid
                self._heartbeat_thread = mt.Thread(target = self._heartbeat_sender,
                                                   args   =[self._heartbeat_term],
                                                   name   = self._heartbeat_tname)
                self._heartbeat_thread.start()


        self._log.debug('start_bridges done')


    # --------------------------------------------------------------------------
    #
    def _start_components(self):

        # this method can only be called from the main thread
        self_thread = mt.current_thread()
        assert(self_thread.name == 'MainThread')
        
        # at this point we know that bridges have been started, and we can use
        # the control pubsub for heartbeat messages.

        self._log.debug('start comps: %s', self._comp_cfg)
      # print 'start comps: %s' % self._comp_cfg

        if not self._comp_cfg:
            return

        assert('heart'   in self._ctrl_cfg)
        assert('bridges' in self._ctrl_cfg )

        assert(rpc.LOG_PUBSUB     in self._ctrl_cfg['bridges'])
        assert(self._ctrl_cfg['bridges'][rpc.LOG_PUBSUB].get('addr_in'))
        assert(self._ctrl_cfg['bridges'][rpc.LOG_PUBSUB].get('addr_out'))

        assert(rpc.CONTROL_PUBSUB in self._ctrl_cfg['bridges'])
        assert(self._ctrl_cfg['bridges'][rpc.CONTROL_PUBSUB].get('addr_in'))
        assert(self._ctrl_cfg['bridges'][rpc.CONTROL_PUBSUB].get('addr_out'))

        # start components
        comps = list()
        for cname,cnum in self._comp_cfg.iteritems():

            self._log.debug('start %s component(s) %s', cnum, cname)

            ctype = self._typemap.get(cname)
            if not ctype:
                raise ValueError('unknown component type (%s)' % cname)

            for i in range(cnum):

                # for components, we pass on the original cfg (or rather a copy
                # of that) -- but we'll overwrite any relevant settings from our
                # ctrl_cfg, soch as bridge addresses, heartbeat configguration,
                # etc.
                ccfg = copy.deepcopy(self._cfg)
                ccfg['components'] = dict()  # avoid recursion
                ccfg['cname']      = cname
                ccfg['number']     = i
                ccfg['owner']      = self._owner

                ru.dict_merge(ccfg, self._ctrl_cfg, ru.OVERWRITE)

                comp = ctype.create(ccfg, self._session)
                comp.start()

                comps.append(comp)

        # components are started -- get them added to the watcher
        self.add_watchables(comps)


    # --------------------------------------------------------------------------
    #
    def add_watchables(self, things, owner=None):
        """
        This method allows to inject objects into the set of things this
        controller is watching.  Only those object types can be added which
        expose pull(), stop() and join() methods.
        Such objects would be components, sub-agents and bridges.
        """

        # this method can only be called from the main thread
        self_thread = mt.current_thread()
        assert(self_thread.name == 'MainThread')
        
        # For a given set of things, add the thing to the list of watch items,
        # which will be automatically be picked up by the watcher thread (which
        # we start also, once).
        #
        # Anything being added here needs: a 'name' property, a 'poll' method
        # (for the watcher to check state), and 'stop' and 'join' methods (to 
        # shut the thing down on termination).
        #
        # Once control is passed to this controller, the callee is not supposed
        # to handle the goven things anymore

        if not owner:
            owner = self._owner

        if not isinstance(things, list):
            things = [things]

        for thing in things:
            assert(thing.name)
            assert(thing.poll)
            assert(thing.stop)
            assert(thing.join)

        # the things are assumed started at this point -- we can start
        # monitoring them.  We may have done so before, so check
        if not self._watcher_thread:
            self._log.debug('create watcher thread')
            self._watcher_term   = mt.Event()
            self._watcher_thread = mt.Thread(target=self._watcher,
                                             args=[self._watcher_term],
                                             name='%s.watcher' % self._uid)
            self._watcher_thread.start()

        # make sure the watcher picks up the right things
        self._components_to_watch += things

        self._log.debug('controller startup completed')


    # --------------------------------------------------------------------------
    #
    def _watcher(self, term):
        """
        we do a poll() on all our bridges, components, and workers,
        to check if they are still alive.  If any goes AWOL, we will begin to
        tear down this agent.
        """

        while not term.is_set() and not self._term.is_set():

          # self._log.debug('watching %s things' % len(to_watch))
          # self._log.debug('watching %s' % pprint.pformat(to_watch))

            # NOTE: these loops rely on the _*_to_watch lists to only ever 
            #       expand, but never to shrink.
            things = (self._components_to_watch + self._bridges_to_watch)
                    
            for thing in things:

                if thing.poll() == None:
                  # self._log.debug('%-40s: ok' % thing.name)
                    pass
                else:
                  # print '%s died? - shutting down' % thing.name
                    self._log.error('%s died - shutting down' % thing.name)

                    # we raise a ru.ThreadExit exception in the main thread
                    ru.raise_in_thread()

                    # the thread finishes here
                    return

            time.sleep(0.1)


    # --------------------------------------------------------------------------
    #
    def _heartbeat_sender(self, term):

        # we use a loop which runs quicker than self._heartbeat_interval would
        # make you think.  This way we can check more frequently for any
        # terminate signal, and thus don't have to delay thread cancellation.

        heart = self._ctrl_cfg['heart']
        addr  = self._ctrl_cfg['bridges'][rpc.CONTROL_PUBSUB]['addr_in']
        pub   = rpu_Pubsub(self._session, rpc.CONTROL_PUBSUB, rpu_PUBSUB_PUB,
                           self._ctrl_cfg, addr=addr)

        last_heartbeat = 0.0  # we never sent a heartbeat before
        while not term.is_set() and not self._term.is_set():

            now = time.time()
            if last_heartbeat + self._heartbeat_interval < now:

              # print 'send heartbeat!!! %s' % heart

                pub.put(rpc.CONTROL_PUBSUB, {'cmd' : 'heartbeat',
                                             'arg' : {'sender' : heart}})
                self._log.debug('heartbeat sent (%s)' % heart)
                last_heartbeat = now

            time.sleep(0.1)


# ------------------------------------------------------------------------------

