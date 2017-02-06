"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
import logging
import copy

from cryptography.fernet import Fernet
from abc import abstractmethod

from yosai.core import (
    AdditionalAuthenticationRequired,
    AuthenticationException,
    DefaultAuthenticator,
    DelegatingSubject,
    EventLogger,
    NativeSessionManager,
    SessionKey,
    SubjectContext,
    SubjectStore,
    InvalidSessionException,
    ModularRealmAuthorizer,
    RememberMeSettings,
    event_bus,
    mgt_abcs,
)

logger = logging.getLogger(__name__)


class AbstractRememberMeManager(mgt_abcs.RememberMeManager):
    """
    Abstract implementation of the ``RememberMeManager`` interface that handles
    serialization and encryption of the remembered user identity.

    The remembered identity storage location and details are left to
    subclasses.

    Default encryption key
    -----------------------
    This implementation uses the Fernet API from PyCA's cryptography for
    symmetric encryption. As per the documentation, Fernet uses AES in CBC mode
    with a 128-bit key for encryption and uses PKCS7 padding:
        https://cryptography.io/en/stable/fernet/

    It also uses a default, generated symmetric key to both encrypt and decrypt
    data.  As AES is a symmetric cipher, the same key is used to both encrypt
    and decrypt data, BUT NOTE:

    Because Yosai is an open-source project, if anyone knew that you were
    using Yosai's default key, they could download/view the source, and with
    enough effort, reconstruct the key and decode encrypted data at will.

    Of course, this key is only really used to encrypt the remembered
    ``IdentifierCollection``, which is typically a user id or username.  So if you
    do not consider that sensitive information, and you think the default key
    still makes things 'sufficiently difficult', then you can ignore this
    issue.

    However, if you do feel this constitutes sensitive information, it is
    recommended that you provide your own key and set it via the cipher_key
    property attribute to a key known only to your application,
    guaranteeing that no third party can decrypt your data.

    You can generate your own key by importing fernet and calling its
    generate_key method:
       >>> from cryptography.fernet import Fernet
       >>> key = Fernet.generate_key()

    your key will be a byte string that looks like this:
        b'cghiiLzTI6CUFCO5Hhh-5RVKzHTQFZM2QSZxxgaC6Wo='

        copy and paste YOUR newly generated byte string, excluding the
        bytestring notation, into its respective place in /conf/yosai.core.settings.json
        following this format:
            default_cipher_key = "cghiiLzTI6CUFCO5Hhh-5RVKzHTQFZM2QSZxxgaC6Wo="
    """

    def __init__(self, settings):

        default_cipher_key = RememberMeSettings(settings).default_cipher_key

        # new to yosai.core.
        self.serialization_manager = None  # it will be injected

        # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!i!!!!!!!!
        # !!!
        #
        #
        #      888
        #      888
        #      888
        #  .d88888    8888b.  88888b.     .d88b.      .d88b.   888 888b
        # d88" 888        "88 b888 "88b   d88P"88b    d8P  Y8  b888P"``
        # 888  888    .d88888 8888  888   888  888    8888888  8888
        # Y88b 888    888  88 8888  888   Y88b 888    Y8b.     888
        #  "Y88888    "Y88888 8888  888   "Y888888    "Y8888   888
        #                                     8888
        #                                     .Y8b
        #                                  "Y88P"
        #
        #                        HEY  YOU!
        # !!! Generate your own cipher key using the instructions above and
        # !!! update your yosai settings file to include it.  The code below
        # !!! references this key.  Yosai does not include a default key.
        # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

        # as default, the encryption key == decryption key:
        self.encryption_cipher_key = default_cipher_key
        self.decryption_cipher_key = default_cipher_key

    @abstractmethod
    def forget_identity(self, subject):
        """
        Forgets (removes) any remembered identity data for the specified
        Subject instance.

        :param subject: the subject instance for which identity data should be
                        forgotten from the underlying persistence mechanism
        """
        pass

    def on_successful_login(self, subject, authc_token, account_id):
        """
        Reacts to the successful login attempt by first always
        forgetting any previously stored identity.  Then if the authc_token
        is a ``RememberMe`` type of token, the associated identity
        will be remembered for later retrieval during a new user session.

        :param subject: the subject whose identifying attributes are being
                        remembered
        :param authc_token:  the token that resulted in a successful
                             authentication attempt
        :param account_id: id of authenticated account
        """
        # always clear any previous identity:
        self.forget_identity(subject)

        # now save the new identity:
        if authc_token.is_remember_me:
            self.remember_identity(subject, authc_token, account_id)
        else:
            msg = ("AuthenticationToken did not indicate that RememberMe is "
                   "requested.  RememberMe functionality will not be executed "
                   "for corresponding account.")
            logger.debug(msg)

    def remember_identity(self, subject, authc_token, account_id):
        """
        Yosai consolidates rememberIdentity, an overloaded method in java,
        to a method that will use an identifier-else-account logic.

        Remembers a subject-unique identity for retrieval later.  This
        implementation first resolves the exact identifying attributes to
        remember.  It then remembers these identifying attributes by calling
            remember_identity(Subject, IdentifierCollection)

        :param subject:  the subject for which the identifying attributes are
                         being remembered
        :param authc_token:  ignored in the AbstractRememberMeManager
        :param account_id: the account id of authenticated account
        """
        try:
            identifiers = self.get_identity_to_remember(subject, account_id)
        except AttributeError:
            msg = "Neither account_id nor identifier arguments passed"
            raise AttributeError(msg)
        encrypted = self.convert_identifiers_to_bytes(identifiers)
        self.remember_encrypted_identity(subject, encrypted)

    def get_identity_to_remember(self, subject, account_id):
        """
        Returns the account's identifier and ignores the subject argument

        :param subject: the subject whose identifiers are remembered
        :param account: the account resulting from the successful authentication attempt
        :returns: the IdentifierCollection to remember
        """
        # This is a placeholder.  A more meaningful logic is implemented by subclasses
        return account_id

    def convert_identifiers_to_bytes(self, identifiers):
        """
        Encryption requires a binary type as input, so this method converts
        the identifier collection object to one.

        :type identifiers: a serializable IdentifierCollection object
        :returns: a bytestring
        """

        # serializes to bytes by default:
        return self.encrypt(self.serialization_manager.serialize(identifiers))

    @abstractmethod
    def remember_encrypted_identity(subject, encrypted):
        """
        Persists the identity bytes to a persistent store

        :param subject: the Subject for whom the identity is being serialized
        :param serialized: the serialized bytes to be persisted.
        """
        pass

    def get_remembered_identifiers(self, subject_context):
        identifiers = None
        try:
            encrypted = self.get_remembered_encrypted_identity(subject_context)
            if encrypted:
                identifiers = self.convert_bytes_to_identifiers(encrypted,
                                                                subject_context)
        except Exception as ex:
            identifiers = \
                self.on_remembered_identifiers_failure(ex, subject_context)

        return identifiers

    @abstractmethod
    def get_remembered_encrypted_identity(subject_context):
        """
        Based on the given subject context data, retrieves the previously
        persisted serialized identity, or None if there is no available data.

        :param subject_context: the contextual data, that
                                is being used to construct a Subject instance.

        :returns: the previously persisted serialized identity, or None if
                  no such data can be acquired for the Subject
        """
        pass

    def convert_bytes_to_identifiers(self, encrypted, subject_context):
        """
        If a cipher_service is available, it will be used to first decrypt the
        serialized message.  Then, the bytes are deserialized and returned.

        :param serialized:      the bytes to decrypt and then deserialize
        :param subject_context: the contextual data, that is being
                                used to construct a Subject instance
        :returns: the de-serialized identifier
        """

        # unlike Shiro, Yosai assumes that the message is encrypted:
        decrypted = self.decrypt(encrypted)

        return self.serialization_manager.deserialize(decrypted)

    def on_remembered_identifiers_failure(self, exc, subject_context):
        """
        Called when an exception is thrown while trying to retrieve identifier.
        The default implementation logs a debug message and forgets ('unremembers')
        the problem identity by calling forget_identity(subject_context) and
        then immediately re-raises the exception to allow the calling
        component to react accordingly.

        This method implementation never returns an object - it always rethrows,
        but can be overridden by subclasses for custom handling behavior.

        This most commonly would be called when an encryption key is updated
        and old identifier are retrieved that have been encrypted with the
        previous key.

        :param exc: the exception that was thrown
        :param subject_context: the contextual data that is being
                                used to construct a Subject instance
        :raises:  the original Exception passed is propagated in all cases
        """
        msg = ("There was a failure while trying to retrieve remembered "
               "identifier.  This could be due to a configuration problem or "
               "corrupted identifier.  This could also be due to a recently "
               "changed encryption key.  The remembered identity will be "
               "forgotten and not used for this request. ", exc)
        logger.debug(msg)

        self.forget_identity(subject_context)

        # propagate - security manager implementation will handle and warn
        # appropriately:
        raise exc

    def encrypt(self, serialized):
        """
        Encrypts the serialized message using Fernet

        :param serialized: the serialized object to encrypt
        :type serialized: bytes
        :returns: an encrypted bytes returned by Fernet
        """

        fernet = Fernet(self.encryption_cipher_key)
        return fernet.encrypt(serialized)

    def decrypt(self, encrypted):
        """
        decrypts the encrypted message using Fernet

        :param encrypted: the encrypted message
        :returns: the decrypted, serialized identifier collection
        """
        fernet = Fernet(self.decryption_cipher_key)
        return fernet.decrypt(encrypted)

    def on_failed_login(self, subject, authc_token, ae):
        """
        Reacts to a failed login by immediately forgetting any previously
        remembered identity.  This is an additional security feature to prevent
        any remenant identity data from being retained in case the
        authentication attempt is not being executed by the expected user.

        :param subject: the subject which executed the failed login attempt
        :param authc_token:   the authentication token resulting in a failed
                              login attempt - ignored by this implementation
        :param ae:  the exception thrown as a result of the failed login
                    attempt - ignored by this implementation
        """
        self.forget_identity(subject)

    def on_logout(self, subject):
        """
        Reacts to a subject logging out of the application and immediately
        forgets any previously stored identity and returns.

        :param subject: the subject logging out
        """
        self.forget_identity(subject)


# also known as ApplicationSecurityManager in Shiro 2.0 alpha:
class NativeSecurityManager(mgt_abcs.SecurityManager):

    def __init__(self,
                 yosai,
                 settings,
                 realms=None,
                 cache_handler=None,
                 authenticator=None,
                 authorizer=ModularRealmAuthorizer(),
                 serialization_manager=None,
                 session_manager=None,
                 remember_me_manager=None,
                 subject_store=SubjectStore()):

        self.yosai = yosai
        self.subject_store = subject_store
        self.realms = realms
        self.remember_me_manager = remember_me_manager

        if not session_manager:
            session_manager = NativeSessionManager(settings)
        self.session_manager = session_manager

        self.authorizer = authorizer

        if not authenticator:
            authenticator = DefaultAuthenticator(settings)
        self.authenticator = authenticator

        if serialization_manager and self.remember_me_manager:
            self.remember_me_manager.serialization_manager = serialization_manager

        self.event_logger = EventLogger(event_bus)
        self.apply_event_bus(event_bus)

        self.apply_cache_handler(cache_handler)
        self.apply_realms()

    def apply_cache_handler(self, cache_handler):
        for realm in self.realms:
            if hasattr(realm, 'cache_handler'):  # implies cache support
                realm.cache_handler = cache_handler
        if hasattr(self.session_manager, 'apply_cache_handler'):
            self.session_manager.apply_cache_handler(cache_handler)

    def apply_event_bus(self, eventbus):
        self.authenticator.event_bus = eventbus
        self.authorizer.event_bus = eventbus
        self.session_manager.apply_event_bus(eventbus)

    def apply_realms(self):
        """
        :realm_s: an immutable collection of one or more realms
        :type realm_s: tuple
        """
        self.authenticator.init_realms(self.realms)
        self.authorizer.init_realms(self.realms)

    def is_permitted(self, identifiers, permission_s):
        """
        :type identifiers: SimpleIdentifierCollection

        :param permission_s: a collection of 1..N permissions
        :type permission_s: List of Permission object(s) or String(s)

        :returns: a List of tuple(s), containing the Permission and a Boolean
                  indicating whether the permission is granted
        """
        return self.authorizer.is_permitted(identifiers, permission_s)

    def is_permitted_collective(self, identifiers, permission_s, logical_operator):
        """
        :type identifiers: SimpleIdentifierCollection

        :param permission_s: a collection of 1..N permissions
        :type permission_s: List of Permission object(s) or String(s)

        :param logical_operator:  indicates whether all or at least one
                                  permission check is true (any)
        :type: any OR all (from python standard library)

        :returns: a Boolean
        """
        return self.authorizer.is_permitted_collective(identifiers,
                                                       permission_s,
                                                       logical_operator)

    def check_permission(self, identifiers, permission_s, logical_operator):
        """
        :type identifiers: SimpleIdentifierCollection

        :param permission_s: a collection of 1..N permissions
        :type permission_s: List of Permission objects or Strings

        :param logical_operator:  indicates whether all or at least one
                                  permission check is true (any)
        :type: any OR all (from python standard library)

        :returns: a List of Booleans corresponding to the permission elements
        """
        return self.authorizer.check_permission(identifiers,
                                                permission_s,
                                                logical_operator)

    def has_role(self, identifiers, role_s):
        """
        :type identifiers: SimpleIdentifierCollection

        :param role_s: 1..N role identifiers (strings)
        :type role_s:  Set of Strings

        :returns: a set of tuple(s), containing the role and a Boolean
                  indicating whether the user is a member of the Role
        """
        return self.authorizer.has_role(identifiers, role_s)

    def has_role_collective(self, identifiers, role_s, logical_operator):
        """
        :type identifiers: SimpleIdentifierCollection

        :param logical_operator:  indicates whether all or at least one
                                  permission check is true (any)
        :type: any OR all (from python standard library)

        :param role_s: 1..N role identifier
        :type role_s:  a Set of Strings

        :returns: a Boolean
        """
        return self.authorizer.has_role_collective(identifiers,
                                                   role_s, logical_operator)

    def check_role(self, identifiers, role_s, logical_operator):
        """
        :type identifiers: SimpleIdentifierCollection

        :param role_s: 1..N role identifier
        :type role_s:  a Set of Strings

        :param logical_operator:  indicates whether all or at least one
                                  permission check is true (any)
        :type: any OR all (from python standard library)

        :raises UnauthorizedException: if Subject not assigned to all roles
        """
        return self.authorizer.check_role(identifiers,
                                          role_s, logical_operator)

    """
    * ===================================================================== *
    * SessionManager Methods                                                *
    * ===================================================================== *
    """
    def start(self, session_context):
        return self.session_manager.start(session_context)

    def get_session(self, session_key):
        return self.session_manager.get_session(session_key)

    """
    * ===================================================================== *
    * SecurityManager Methods                                               *
    * ===================================================================== *
    """

    # existing_subject is used by WebSecurityManager:
    def create_subject_context(self, existing_subject):
        if not hasattr(self, 'yosai'):
            msg = "SecurityManager has no Yosai attribute set."
            raise AttributeError(msg)
        return SubjectContext(self.yosai, self)

    def create_subject(self,
                       authc_token=None,
                       account_id=None,
                       existing_subject=None,
                       subject_context=None):
        """
        Creates a ``Subject`` instance for the user represented by the given method
        arguments.

        It is an overloaded method, due to porting java to python, and is
        consequently highly likely to be refactored.

        It gets called in one of two ways:
        1) when creating an anonymous subject, passing create_subject
           a subject_context argument

        2) following a after successful login, passing all but the context argument

        This implementation functions as follows:

        - Ensures that the ``SubjectContext`` exists and is as populated as it can be,
          using heuristics to acquire data that may not have already been available
          to it (such as a referenced session or remembered identifiers).
        - Calls subject_context.do_create_subject to perform the Subject
          instance creation
        - Calls subject.save to ensure the constructed Subject's state is
          accessible for future requests/invocations if necessary
        - Returns the constructed Subject instance

        :type authc_token:  subject_abcs.AuthenticationToken

        :param account_id:  the identifiers of a newly authenticated user
        :type account:  SimpleIdentifierCollection

        :param existing_subject: the existing Subject instance that initiated the
                                 authentication attempt
        :type subject:  subject_abcs.Subject

        :type subject_context:  subject_abcs.SubjectContext

        :returns:  the Subject instance that represents the context and session
                   data for the newly authenticated subject
        """
        if subject_context is None:  # this that means a successful login just happened
            # passing existing_subject is new to yosai:
            context = self.create_subject_context(existing_subject)

            context.authenticated = True
            context.authentication_token = authc_token
            context.account_id = account_id

            if (existing_subject):
                context.subject = existing_subject

        else:
            context = copy.copy(subject_context)  # if this necessary? TBD.

        context = self.ensure_security_manager(context)
        context = self.resolve_session(context)
        context = self.resolve_identifiers(context)

        subject = self.do_create_subject(context)  # DelegatingSubject

        # save this subject for future reference if necessary:
        # (this is needed here in case remember_me identifiers were resolved
        # and they need to be stored in the session, so we don't constantly
        # re-hydrate the remember_me identifier_collection on every operation).
        self.save(subject)
        return subject

    def update_subject_identity(self, account_id, subject):
        subject.identifiers = account_id
        self.save(subject)
        return subject

    def remember_me_successful_login(self, authc_token, account_id, subject):
        rmm = self.remember_me_manager
        if (rmm is not None):
            try:
                rmm.on_successful_login(subject, authc_token, account_id)
            except Exception:
                msg = ("Delegate RememberMeManager instance of type [" +
                       rmm.__class__.__name__ + "] threw an exception "
                       + "during on_successful_login.  RememberMe services "
                       + "will not be performed for account_id [" + str(account_id) +
                       "].")
                logger.warning(msg, exc_info=True)

        else:

            msg = ("This " + rmm.__class__.__name__ +
                   " instance does not have a [RememberMeManager] instance " +
                   "configured.  RememberMe services will not be performed " +
                   "for account_id [" + str(account_id) + "].")
            logger.info(msg)

    def remember_me_failed_login(self, authc_token, authc_exc, subject):
        rmm = self.remember_me_manager
        if (rmm is not None):
            try:
                rmm.on_failed_login(subject, authc_token, authc_exc)

            except Exception:
                msg = ("Delegate RememberMeManager instance of type "
                       "[" + rmm.__class__.__name__ + "] threw an exception "
                       "during on_failed_login for AuthenticationToken [" +
                       str(authc_token) + "].")
                logger.warning(msg, exc_info=True)

    def remember_me_logout(self, subject):
        rmm = self.remember_me_manager
        if (rmm is not None):
            try:
                rmm.on_logout(subject)
            except Exception as ex:
                msg = ("Delegate RememberMeManager instance of type [" +
                       rmm.__class__.__name__ + "] threw an exception during "
                       "on_logout for subject with identifiers [{identifiers}]".
                       format(identifiers=subject.identifiers if subject else None))
                logger.warning(msg, exc_info=True)

    def login(self, subject, authc_token):
        """
        Login authenticates a user using an AuthenticationToken.  If authentication is
        successful AND the Authenticator has determined that authentication is
        complete for the account, login constructs a Subject instance representing
        the authenticated account's identity. Once a subject instance is constructed,
        it is bound to the application for subsequent access before being returned
        to the caller.

        If login successfully authenticates a token but the Authenticator has
        determined that subject's account isn't considered authenticated,
        the account is configured for multi-factor authentication.

        Sessionless environments must pass all authentication tokens to login
        at once.

        :param authc_token: the authenticationToken to process for the login attempt
        :type authc_token:  authc_abcs.authenticationToken

        :returns: a Subject representing the authenticated user
        :raises AuthenticationException:  if there is a problem authenticating
                                          the specified authc_token
        :raises AdditionalAuthenticationRequired: during multi-factor authentication
                                                  when additional tokens are required
        """
        try:
            # account_id is a SimpleIdentifierCollection
            account_id = self.authenticator.authenticate_account(subject.identifiers,
                                                                 authc_token)
        # implies multi-factor authc not complete:
        except AdditionalAuthenticationRequired as exc:
            # identity needs to be accessible for subsequent authentication:
            self.update_subject_identity(exc.account_id, subject)
            # no need to propagate account further:
            raise AdditionalAuthenticationRequired

        except AuthenticationException as authc_ex:
            try:
                self.on_failed_login(authc_token, authc_ex, subject)
            except Exception:
                msg = ("on_failed_login method raised an exception.  Logging "
                       "and propagating original AuthenticationException.")
                logger.info(msg, exc_info=True)
            raise

        logged_in = self.create_subject(authc_token=authc_token,
                                        account_id=account_id,
                                        existing_subject=subject)
        self.on_successful_login(authc_token, account_id, logged_in)
        return logged_in

    def on_successful_login(self, authc_token, account_id, subject):
        self.remember_me_successful_login(authc_token, account_id, subject)

    def on_failed_login(self, authc_token, authc_exc, subject):
        self.remember_me_failed_login(authc_token, authc_exc, subject)

    def before_logout(self, subject):
        self.remember_me_logout(subject)

    def do_create_subject(self, subject_context):
        """
        By the time this method is invoked, all possible
        ``SubjectContext`` data (session, identifiers, et. al.) has been made
        accessible using all known heuristics.

        :returns: a Subject instance reflecting the data in the specified
                  SubjectContext data map
        """
        security_manager = subject_context.resolve_security_manager()
        session = subject_context.resolve_session()
        session_creation_enabled = subject_context.session_creation_enabled

        # passing the session arg is new to yosai, eliminating redunant
        # get_session calls:
        identifiers = subject_context.resolve_identifiers(session)
        remembered = getattr(subject_context, 'remembered', False)
        authenticated = subject_context.resolve_authenticated(session)
        host = subject_context.resolve_host(session)

        return DelegatingSubject(identifiers=identifiers,
                                 remembered=remembered,
                                 authenticated=authenticated,
                                 host=host,
                                 session=session,
                                 session_creation_enabled=session_creation_enabled,
                                 security_manager=security_manager)

    def save(self, subject):
        """
        Saves the subject's state to a persistent location for future reference.
        This implementation merely delegates saving to the internal subject_store.
        """
        self.subject_store.save(subject)

    def delete(self, subject):
        """
        This method removes (or 'unbinds') the Subject's state from the
        application, typically called during logout.

        This implementation merely delegates deleting to the internal subject_store.

        :param subject: the subject for which state will be removed
        """
        self.subject_store.delete(subject)

    def ensure_security_manager(self, subject_context):
        """
        Determines whether there is a ``SecurityManager`` instance in the context,
        and if not, adds 'self' to the context.  This ensures that do_create_subject
        will have access to a ``SecurityManager`` during Subject construction.

        :param subject_context: the subject context data that may contain a
                                SecurityManager instance
        :returns: the SubjectContext
        """
        if (subject_context.resolve_security_manager() is not None):
            msg = ("Subject Context resolved a security_manager "
                   "instance, so not re-assigning.  Returning.")
            logger.debug(msg)
            return subject_context

        msg = ("No security_manager found in context.  Adding self "
               "reference.")
        logger.debug(msg)

        subject_context.security_manager = self

        return subject_context

    def resolve_session(self, subject_context):
        """
        This method attempts to resolve any associated session based on the
        context and returns a context that represents this resolved Session to
        ensure it may be referenced, if needed, by the invoked do_create_subject
        that performs actual ``Subject`` construction.

        If there is a ``Session`` already in the context (because that is what the
        caller wants to use for Subject construction) or if no session is
        resolved, this method effectively does nothing, returning an
        unmodified context as it was received by the method.

        :param subject_context: the subject context data that may resolve a
                                Session instance
        :returns: the context
        """
        if (subject_context.resolve_session() is not None):
            msg = ("Context already contains a session.  Returning.")
            logger.debug(msg)
            return subject_context

        try:
            # Context couldn't resolve it directly, let's see if we can
            # since we  have direct access to the session manager:
            session = self.resolve_context_session(subject_context)

            # if session is None, given that subject_context.session
            # is None there is no harm done by setting it to None again
            subject_context.session = session

        except InvalidSessionException:
            msg = ("Resolved subject_subject_context context session is "
                   "invalid.  Ignoring and creating an anonymous "
                   "(session-less) Subject instance.")
            logger.debug(msg, exc_info=True)

        return subject_context

    def resolve_context_session(self, subject_context):
        session_key = self.get_session_key(subject_context)

        if (session_key is not None):
            return self.get_session(session_key)

        return None

    def get_session_key(self, subject_context):
        session_id = subject_context.session_id
        if (session_id is not None):
            return SessionKey(session_id)
        return None

    # yosai.core.omits is_empty method

    def resolve_identifiers(self, subject_context):
        """
        ensures that a subject_context has identifiers and if it doesn't will
        attempt to locate them using heuristics
        """
        session = subject_context.session
        identifiers = subject_context.resolve_identifiers(session)

        if (not identifiers):
            msg = ("No identity (identifier_collection) found in the "
                   "subject_context.  Looking for a remembered identity.")
            logger.debug(msg)

            identifiers = self.get_remembered_identity(subject_context)

            if identifiers:
                msg = ("Found remembered IdentifierCollection.  Adding to the "
                       "context to be used for subject construction.")
                logger.debug(msg)

                subject_context.identifiers = identifiers
                subject_context.remembered = True

            else:
                msg = ("No remembered identity found.  Returning original "
                       "context.")
                logger.debug(msg)

        return subject_context

    def create_session_context(self, subject_context):
        session_context = {}

        if (not subject_context.is_empty):
            session_context.update(subject_context.__dict__)

        session_id = subject_context.session_id
        if (session_id):
            session_context['session_id'] = session_id

        host = subject_context.resolve_host(None)
        if (host):
            session_context['host'] = host

        return session_context

    def logout(self, subject):
        """
        Logs out the specified Subject from the system.

        Note that most application developers should not call this method unless
        they have a good reason for doing so.  The preferred way to logout a
        Subject is to call ``Subject.logout()``, not by calling ``SecurityManager.logout``
        directly. However, framework developers might find calling this method
        directly useful in certain cases.

        :param subject the subject to log out:
        :type subject:  subject_abcs.Subject
        """
        if (subject is None):
            msg = "Subject argument cannot be None."
            raise ValueError(msg)

        self.before_logout(subject)

        identifiers = copy.copy(subject.identifiers)   # copy is new to yosai
        if (identifiers):
            msg = ("Logging out subject with primary identifier {0}".format(
                   identifiers.primary_identifier))
            logger.debug(msg)

        try:
            # this removes two internal attributes from the session:
            self.delete(subject)
        except Exception:
            msg = "Unable to cleanly unbind Subject.  Ignoring (logging out)."
            logger.debug(msg, exc_info=True)

        finally:
            try:
                self.stop_session(subject)
            except Exception:
                msg2 = ("Unable to cleanly stop Session for Subject. "
                        "Ignoring (logging out).")
                logger.debug(msg2, exc_info=True)

    def stop_session(self, subject):
        session = subject.get_session(False)
        if (session):
            session.stop(subject.identifiers)

    def get_remembered_identity(self, subject_context):
        """
        Using the specified subject context map intended to build a ``Subject``
        instance, returns any previously remembered identifiers for the subject
        for automatic identity association (aka 'Remember Me').
        """
        rmm = self.remember_me_manager
        if rmm is not None:
            try:
                return rmm.get_remembered_identifiers(subject_context)
            except Exception as ex:
                msg = ("Delegate RememberMeManager instance of type [" +
                       rmm.__class__.__name__ + "] raised an exception during "
                       "get_remembered_identifiers().")
                logger.warning(msg, exc_info=True)
        return None
