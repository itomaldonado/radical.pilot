
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"


import os
import shutil

import radical.utils as ru

from .... import pilot     as rp
from ...  import utils     as rpu
from ...  import states    as rps
from ...  import constants as rpc

from .base import UMGRStagingInputComponent


# ==============================================================================
#
class Default(UMGRStagingInputComponent):

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        rpu.Component.__init__(self, rpc.UMGR_STAGING_INPUT_COMPONENT, cfg, session)


    # --------------------------------------------------------------------------
    #
    def initialize_child(self):

        self.declare_input(rps.UMGR_STAGING_INPUT_PENDING,
                           rpc.UMGR_STAGING_INPUT_QUEUE, self.work)

        self.declare_output(rps.ALLOCATING_PENDING, rpc.UMGR_SCHEDULING_QUEUE)

        self.declare_publisher('state', rpc.STATE_PUBSUB)

        # all components use the command channel for control messages
        self.declare_publisher ('command', rpc.COMMAND_PUBSUB)

        # communicate successful startup
        self.publish('command', {'cmd' : 'alive',
                                 'arg' : self.cname})


    # --------------------------------------------------------------------------
    #
    def finalize_child(self):

        # communicate finalization
        self.publish('command', {'cmd' : 'final',
                                 'arg' : self.cname})



    # --------------------------------------------------------------------------
    #
    def work(self, cu):

        self.advance(cu, rps.UMGR_STAGING_INPUT, publish=True, push=False)
        self._log.info('handle %s' % cu['_id'])

        workdir      = os.path.join(self._cfg['workdir'], '%s' % cu['_id'])
        gtod         = os.path.join(self._cfg['workdir'], 'gtod')
        staging_area = os.path.join(self._cfg['workdir'], self._cfg['staging_area'])
        staging_ok   = True

        cu['workdir']     = workdir
        cu['stdout']      = ''
        cu['stderr']      = ''
        cu['opaque_clot'] = None
        # TODO: See if there is a more central place to put this
        cu['gtod']        = gtod

        stdout_file       = cu['description'].get('stdout')
        stdout_file       = stdout_file if stdout_file else 'STDOUT'
        stderr_file       = cu['description'].get('stderr')
        stderr_file       = stderr_file if stderr_file else 'STDERR'

        cu['stdout_file'] = os.path.join(workdir, stdout_file)
        cu['stderr_file'] = os.path.join(workdir, stderr_file)

        # create unit workdir
        rpu.rec_makedir(workdir)
        self._prof.prof('unit mkdir', uid=cu['_id'])

        try:
            for directive in cu['UMGR_Input_Directives']:

                self._prof.prof('UMGR input_staging queue', uid=cu['_id'],
                         msg="%s -> %s" % (str(directive['source']), str(directive['target'])))

                # Perform input staging
                self._log.info("unit input staging directives %s for cu: %s to %s",
                               directive, cu['_id'], workdir)

                # Convert the source_url into a SAGA Url object
                source_url = rs.Url(directive['source'])

                # Handle special 'staging' scheme
                if source_url.scheme == self._cfg['staging_scheme']:
                    self._log.info('Operating from staging')
                    # Remove the leading slash to get a relative path from the staging area
                    rel2staging = source_url.path.split('/',1)[1]
                    source = os.path.join(staging_area, rel2staging)
                else:
                    self._log.info('Operating from absolute path')
                    source = source_url.path

                # Get the target from the directive and convert it to the location
                # in the workdir
                target = directive['target']
                abs_target = os.path.join(workdir, target)

                # Create output directory in case it doesn't exist yet
                rpu.rec_makedir(os.path.dirname(abs_target))

                self._log.info("Going to '%s' %s to %s", directive['action'], source, abs_target)

                if   directive['action'] == LINK: os.symlink     (source, abs_target)
                elif directive['action'] == COPY: shutil.copyfile(source, abs_target)
                elif directive['action'] == MOVE: shutil.move    (source, abs_target)
                else:
                    # FIXME: implement TRANSFER mode
                    raise NotImplementedError('Action %s not supported' % directive['action'])

                log_message = "%s'ed %s to %s - success" % (directive['action'], source, abs_target)
                self._log.info(log_message)

        except Exception as e:
            self._log.exception("staging input failed -> unit failed")
            staging_ok = False


        # UMGR input staging is done (or failed)
        if staging_ok:
          # self.advance(cu, rps.UMGR_SCHEDULING_PENDING, publish=True, push=True)
            self.advance(cu, rps.ALLOCATING_PENDING, publish=True, push=True)
        else:
            self.advance(cu, rps.FAILED, publish=True, push=False)


# ------------------------------------------------------------------------------
