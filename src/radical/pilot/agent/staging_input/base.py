
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"


import radical.utils as ru

from ... import utils     as rpu
from ... import states    as rps
from ... import constants as rpc


# ------------------------------------------------------------------------------
# 'enum' for RP's agent staging input types
RP_ASI_NAME_DEFAULT = "DEFAULT"


# ==============================================================================
#
class AgentStagingInputComponent(rpu.Component):

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        self._uid = ru.generate_id('agent.staging.input.%(counter)s', ru.ID_CUSTOM)

        rpu.Component.__init__(self, cfg, session)


    # --------------------------------------------------------------------------
    #
    # This class-method creates the appropriate sub-class for the Stager
    #
    @classmethod
    def create(cls, cfg, session):

        name = cfg.get('agent_staging_input_component', RP_ASI_NAME_DEFAULT)

        # Make sure that we are the base-class!
        if cls != AgentStagingInputComponent:
            raise TypeError("Factory only available to base class!")

        from .default import Default

        try:
            impl = {
                RP_ASI_NAME_DEFAULT: Default
            }[name]

            impl = impl(cfg, session)
            return impl

        except KeyError:
            raise ValueError("AgentStagingInputComponent '%s' unknown or defunct" % name)


# ------------------------------------------------------------------------------

