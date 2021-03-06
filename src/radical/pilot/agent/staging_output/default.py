
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"


import os
import shutil

import saga          as rs
import radical.utils as ru

from .... import pilot     as rp
from ...  import utils     as rpu
from ...  import states    as rps
from ...  import constants as rpc

from .base import AgentStagingOutputComponent

from ...staging_directives import complete_url


# ==============================================================================
#
class Default(AgentStagingOutputComponent):
    """
    This component performs all agent side output staging directives for compute
    units.  It gets units from the agent_staging_output_queue, in
    AGENT_STAGING_OUTPUT_PENDING state, will advance them to
    AGENT_STAGING_OUTPUT state while performing the staging, and then moves then
    to the UMGR_STAGING_OUTPUT_PENDING state, which at the moment requires the
    state change to be published to MongoDB (no push into a queue).

    Note that this component also collects stdout/stderr of the units (which
    can also be considered staging, really).
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        AgentStagingOutputComponent.__init__(self, cfg, session)


    # --------------------------------------------------------------------------
    #
    def initialize_child(self):

        self._pwd = os.getcwd()

        self.register_input(rps.AGENT_STAGING_OUTPUT_PENDING, 
                            rpc.AGENT_STAGING_OUTPUT_QUEUE, self.work)

        # we don't need an output queue -- units are picked up via mongodb
        self.register_output(rps.UMGR_STAGING_OUTPUT_PENDING, None) # drop units


    # --------------------------------------------------------------------------
    #
    def work(self, units):

        if not isinstance(units, list):
            units = [units]

        self.advance(units, rps.AGENT_STAGING_OUTPUT, publish=True, push=False)

        ru.raise_on('work bulk')

        # we first filter out any units which don't need any input staging, and
        # advance them again as a bulk.  We work over the others one by one, and
        # advance them individually, to avoid stalling from slow staging ops.
        
        no_staging_units = list()
        staging_units    = list()

        for unit in units:

            uid = unit['uid']

            # From here on, any state update will hand control over to the umgr
            # again.  The next unit update should thus push *all* unit details,
            # not only state.
            unit['$all']    = True 
            unit['control'] = 'umgr_pending'

            # we always dig for stdout/stderr
            self._handle_unit_stdio(unit)

            # NOTE: all units get here after execution, even those which did not
            #       finish successfully.  We do that so that we can make
            #       stdout/stderr available for failed units (see
            #       _handle_unit_stdio above).  But we don't need to perform any
            #       other staging for those units, and in fact can make them
            #       final.
            if unit['target_state'] != rps.DONE:
                unit['state'] = unit['target_state']
                self._log.debug('unit %s skips staging (%s)', uid, unit['state'])
                no_staging_units.append(unit)
                continue

            # check if we have any staging directives to be enacted in this
            # component
            actionables = list()
            for sd in unit['description'].get('output_staging', []):
                if sd['action'] in [rpc.LINK, rpc.COPY, rpc.MOVE]:
                    actionables.append(sd)

            if actionables:
                # this unit needs some staging
                staging_units.append([unit, actionables])
            else:
                # this unit does not need any staging at this point, and can be
                # advanced
                unit['state'] = rps.UMGR_STAGING_OUTPUT_PENDING
                no_staging_units.append(unit)

        if no_staging_units:
            self.advance(no_staging_units, publish=True, push=True)

        for unit,actionables in staging_units:
            self._handle_unit_staging(unit, actionables)


    # --------------------------------------------------------------------------
    #
    def _handle_unit_stdio(self, unit):

        sandbox = unit['unit_sandbox']

        # TODO: disable this at scale?
        if os.path.isfile(unit['stdout_file']):
            with open(unit['stdout_file'], 'r') as stdout_f:
                try:
                    txt = unicode(stdout_f.read(), "utf-8")
                except UnicodeDecodeError:
                    txt = "unit stdout is binary -- use file staging"

                unit['stdout'] += rpu.tail(txt)

        # TODO: disable this at scale?
        if os.path.isfile(unit['stderr_file']):
            with open(unit['stderr_file'], 'r') as stderr_f:
                try:
                    txt = unicode(stderr_f.read(), "utf-8")
                except UnicodeDecodeError:
                    txt = "unit stderr is binary -- use file staging"

                unit['stderr'] += rpu.tail(txt)

        if 'RADICAL_PILOT_PROFILE' in os.environ:
            if os.path.isfile("%s/PROF" % sandbox):
                try:
                    with open("%s/PROF" % sandbox, 'r') as prof_f:
                        txt = prof_f.read()
                        for line in txt.split("\n"):
                            if line:
                                ts, name, uid, state, event, msg = line.split(',')
                                self._prof.prof(name=name, uid=uid, state=state,
                                        event=event, msg=msg, timestamp=float(ts))
                except Exception as e:
                    self._log.error("Pre/Post profile read failed: `%s`" % e)


    # --------------------------------------------------------------------------
    #
    def _handle_unit_staging(self, unit, actionables):

        ru.raise_on('work unit')

        uid = unit['uid']

        # NOTE: see documentation of cu['sandbox'] semantics in the ComputeUnit
        #       class definition.
        sandbox = unit['unit_sandbox']

        # By definition, this compoentn lives on the pilot's target resource.
        # As such, we *know* that all staging ops which would refer to the
        # resource now refer to file://localhost, and thus translate the unit,
        # pilot and resource sandboxes into that scope.  Some assumptions are
        # made though:
        #
        #   * paths are directly translatable across schemas
        #   * resource level storage is in fact accessible via file://
        #
        # FIXME: this is costly and should be cached.

        unit_sandbox     = ru.Url(unit['unit_sandbox'])
        pilot_sandbox    = ru.Url(unit['pilot_sandbox'])
        resource_sandbox = ru.Url(unit['resource_sandbox'])

        unit_sandbox.schema     = 'file'
        pilot_sandbox.schema    = 'file'
        resource_sandbox.schema = 'file'

        unit_sandbox.host       = 'localhost'
        pilot_sandbox.host      = 'localhost'
        resource_sandbox.host   = 'localhost'

        src_context = {'pwd'      : str(unit_sandbox),       # !!!
                       'unit'     : str(unit_sandbox), 
                       'pilot'    : str(pilot_sandbox), 
                       'resource' : str(resource_sandbox)}
        tgt_context = {'pwd'      : str(unit_sandbox),       # !!!
                       'unit'     : str(unit_sandbox), 
                       'pilot'    : str(pilot_sandbox), 
                       'resource' : str(resource_sandbox)}

        # we can now handle the actionable staging directives
        for sd in actionables:

            action = sd['action']
            flags  = sd['flags']
            did    = sd['uid']
            src    = sd['source']
            tgt    = sd['target']

            self._prof.prof('staging_begin', uid=uid, msg=did)

            assert(action in [rpc.COPY, rpc.LINK, rpc.MOVE, rpc.TRANSFER]), \
                              'invalid staging action'

            # we only handle staging which does *not* include 'client://' src or
            # tgt URLs - those are handled by the umgr staging components
            if '://' in src and src.startswith('client://'):
                self._log.debug('skip staging for src %s', src)
                self._prof.prof('staging_end', uid=uid, msg=did)
                continue

            if '://' in tgt and tgt.startswith('client://'):
                self._log.debug('skip staging for tgt %s', tgt)
                self._prof.prof('staging_end', uid=uid, msg=did)
                continue

            src = complete_url(src, src_context, self._log)
            tgt = complete_url(tgt, tgt_context, self._log)

            assert(src.schema == 'file'), 'staging src must be file://'

            if action in [rpc.COPY, rpc.LINK, rpc.MOVE]:
                assert(tgt.schema == 'file'), 'staging tgt expected as file://'


            # SAGA will take care of dir creation - but we do it manually
            # for local ops (copy, link, move)
            if rpc.CREATE_PARENTS in flags and action != rpc.TRANSFER:
                tgtdir = os.path.dirname(tgt.path)
                if tgtdir != sandbox:
                    # TODO: optimization point: create each dir only once
                    self._log.debug("mkdir %s" % tgtdir)
                    rpu.rec_makedir(tgtdir)

            if   action == rpc.COPY: shutil.copyfile(src.path, tgt.path)
            elif action == rpc.LINK: os.symlink     (src.path, tgt.path)
            elif action == rpc.MOVE: shutil.move    (src.path, tgt.path)
            elif action == rpc.TRANSFER:

                # FIXME: we only handle srm staging right now, and only for
                #        a specific target proxy. Other TRANSFER directives are
                #        left to umgr output staging.  We should use SAGA to
                #        attempt all staging ops which do not target the client
                #        machine.
                if tgt.schema == 'srm':
                    # FIXME: cache saga handles
                    srm_dir = rs.filesystem.Directory('srm://proxy/?SFN=bogus')
                    srm_dir.copy(src, tgt)
                    srm_dir.close()
                else:
                    self._log.error('no transfer for %s -> %s', src, tgt)
                    self._prof.prof('staging_end', uid=uid, msg=did)
                    raise NotImplementedError('unsupported transfer %s' % tgt)

            self._prof.prof('staging_end', uid=uid, msg=did)

        # all agent staging is done -- pass on to umgr output staging
        self.advance(unit, rps.UMGR_STAGING_OUTPUT_PENDING, publish=True, push=False)


# ------------------------------------------------------------------------------

