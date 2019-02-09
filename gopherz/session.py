import time
import random
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)


class Session:
    """Backend for storing persistent user sessions.

    Users are stored in shared memory at the global level. This is less complex
    and less resource intensive than setting up a database or Redis backend.
    The catch is that it only works because I'm running a single-process,
    multi-threaded server. It also means that if the server ever restarts,
    everyone loses their existing session.

    There are two distinct user caches:

    unverified_users:
        These are for users who haven't verified their captcha question yet.
        The session is only saved so the backend can keep track of the answer
        to the captcha.

    verified_users:
        These are for users who have verified their identity. The limit on the
        total number of verified sessions is kept low because each user will
        have their own subprocess running frotz.

    Inactive users are periodically evicted to keep memory usage down.
    """

    def __init__(self):
        self.unverified_users = OrderedDict()
        self.unverified_users_limit = 1000          # 1000 users
        self.unverified_users_max_age = 60 * 5      # 5 minute timeout

        self.verified_users = OrderedDict()
        self.verified_users_limit = 20              # 20 users
        self.verified_users_max_age = 60 * 60 * 24  # 24 hour timeout

        # Loop every 5 minutes to check for expired user sessions
        self.evict_interval = 60 * 5

    def exists(self, pk):
        """Check if the user is already saved in the session.
        """
        return pk in self.unverified_users or pk in self.verified_users

    def load(self, pk):
        """Given a user pk, attempt to load the user from the session.
        """
        if pk in self.unverified_users:
            self.unverified_users.move_to_end(pk)
            return self.unverified_users[pk]
        elif pk in self.verified_users:
            self.verified_users.move_to_end(pk)
            return self.verified_users[pk]
        else:
            return None

    def save(self, pk, user):
        """Save a user to the session backend.

        If the user limit is reached for the session cache, the oldest active
        user will be evicted to make room.
        """
        if user.verified:
            logger.info(f'Saving user {pk} as verified')
            self.unverified_users.pop(pk, None)
            self.verified_users[pk] = user
            while len(self.verified_users) > self.verified_users_limit:
                pk, user = self.verified_users.popitem(last=False)
                logger.info(f'Evicting user {pk}')
                self.evict(user)

        else:
            logger.info(f'Saving user {pk} as unverified')
            self.verified_users.pop(pk, None)
            self.unverified_users[pk] = user
            while len(self.unverified_users) > self.unverified_users_limit:
                pk, user = self.unverified_users.popitem(last=False)
                logger.info(f'Evicting user {pk}')
                self.evict(user)

    def evict_forever(self):
        """Loop in a thread and evict inactive users from the session.
        """
        logger.info('Entering evict_forever loop')
        while True:
            time.sleep(self.evict_interval)
            logger.info('Searching for users to evict')
            now = time.time()

            count = len(self.unverified_users)
            logger.info(f'Total {count} unverified users')
            for pk, user in list(self.unverified_users.items()):
                delta = now - user.last_access
                if delta > self.unverified_users_max_age:
                    logger.info(f'Evicting user {pk}, delta {delta:.2f}s')
                    self.unverified_users.pop(pk, None)
                    self.evict(user)
                else:
                    break

            count = len(self.verified_users)
            logger.info(f'Total {count} verified users')
            for pk, user in list(self.verified_users.items()):
                delta = now - user.last_access
                if delta > self.verified_users_max_age:
                    logger.info(f'Evicting user {pk}, delta {delta:.2f}s')
                    self.verified_users.pop(pk, None)
                    self.evict(user)
                else:
                    break

    @staticmethod
    def evict(user):
        if user.frotz:
            user.frotz.close()
            user.frotz = None


class User:

    # Global session state shared among all threads
    session = Session()

    def __init__(self, pk):
        self.pk = pk
        self.verified = False
        self.frotz = None
        self.last_access = None

        self._captcha_question = None
        self._captcha_answer = None

    @property
    def persistent(self):
        """Is the user's state saved in the session backend.
        """
        return self.session.exists(self.pk)

    @property
    def game(self):
        return self.frotz.game if self.frotz else None

    def save(self):
        """Save the user to the session backend.
        """
        self.session.save(self.pk, self)

    @classmethod
    def load(cls, pk):
        """Attempt to load the user from the session backend.

        Will create a new user if the pk doesn't exist.
        """
        user = cls.session.load(pk)
        if not user:
            user = cls(pk)

        user.last_access = time.time()
        return user

    def get_captcha(self):
        """Get a captcha question that the user must solve to verify themselves.
        """
        self.save()
        a, b = random.randint(1, 10), random.randint(1, 10)
        self._captcha_question = f'{a} + {b} = ?'
        self._captcha_answer = a + b
        return self._captcha_question

    def check_captcha(self, answer):
        """Check that the user's captcha response is correct.
        """
        if answer.strip() == str(self._captcha_answer):
            self.verified = True
            self.save()
        return self.verified
