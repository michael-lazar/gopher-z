#!/usr/bin/env python3
import os
import time
import select
import random
import secrets
import logging
import threading
import subprocess
from collections import OrderedDict
from string import ascii_letters, digits

from flask import Flask, request, g
from flask_gopher import GopherRequestHandler, GopherExtension


logger = logging.getLogger('gopher-z')

app = Flask(__name__, static_url_path='')
app.config.from_pyfile('gopher-z.cfg')

gopher = GopherExtension(app)
game_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'games')


class GameEnded(Exception):
    """The underlying frotz process has ended normally or crashed"""


class FrotzProcess:
    """Wrapper around the frotz command line program.

    Installation:
        $ git clone https://gitlab.com/DavidGriffith/frotz/
        $ make dumb
        $ make install_dfrotz
    """
    command = ['dfrotz', '-p', '-w 67', '-S 67']

    games = {
        'tangle': 'Tangle.z5',
        'lost-pig': 'LostPig.z8',
        'zork': 'ZORK1.DAT',
        'planetfall': 'PLANETFA.DAT'
    }

    # Be over-conservative about what characters are allowed in commands
    safe_characters = ascii_letters + digits + '.,?! '
    illegal_commands = ('RESTORE', 'SAVE', 'SCRIPT', 'UNSCRIPT')

    illegal_command_screen = (
        'I\'m sorry, but the "{cmd}" command is not currently implemented\n\n>'
    )

    def __init__(self, game_name):
        self.game_name = game_name
        self.game_file = self.games[game_name]
        self.process = None
        self.last_screen = ''

    def launch(self):
        data_path = os.path.join(game_dir, self.game_file)
        self.process = subprocess.Popen(
            self.command + [data_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0
        )
        pid = self.process.pid
        logger.info(f'Frotz [{pid}] - launched process')
        return self._get_screen()

    def communicate(self, command):
        try:
            return self._communicate(command)
        except OSError:
            pid = self.process.pid
            logger.exception(f'Frotz [{pid}] - communication error')
            raise GameEnded()

    def close(self):
        if self.process:
            pid = self.process.pid
            logger.info(f'Frotz [{pid}] - attempting to kill process')
            try:
                self.process.kill()
            except Exception:
                logger.exception(f'Frotz [{pid}] - error killing process')
            else:
                logger.info(f'Frotz [{pid}] - process killed')
            self.process = None
        self.last_screen = ''

    def _communicate(self, command):
        if not self.process:
            return self.launch()

        if command is None:
            return self.last_screen

        command = self._sanitize(command)
        for cmd in self.illegal_commands:
            if command.startswith(cmd):
                return self.illegal_command_screen.format(cmd=cmd)

        command += '\n'
        self.process.stdin.write(command.encode())
        self.process.stdin.flush()
        return self._get_screen()

    def _get_screen(self):
        lines = []
        while select.select([self.process.stdout], [], [], 0.1)[0]:
            data = self.process.stdout.read(2048)
            if not data:
                pid = self.process.pid
                logger.error(f'Frotz [{pid}] - stdout was empty')
                raise GameEnded()
            lines.append(data)

        screen = b''.join(lines).decode()
        self.last_screen = screen
        return screen

    def _sanitize(self, command):
        command = command[:128]
        command = ''.join(c for c in command if c in self.safe_characters)
        command = command.strip('.,?! ').upper()
        return command


class Session:
    """Container for user sessions.

    Users are stored in-memory at the module level. All request threads have
    access to the shared session memory. This is easier than setting up a
    database or redis backend, but it only works because I'm running a single
    server process in threaded=True mode.

    There are two distinct user caches:

    unverified_users:
        These are for users who haven't verified their captcha question yet.
        The session is only saved so the backend can keep track of the answer
        to the captcha.

    verified_users:
        These are for users who have verified their identity. The limit on the
        total number of verified sessions is kept low because each of these
        might be associated with its own child process running frotz.

    Inactive users are periodically evicted to keep memory usage down.
    """

    def __init__(self):
        self.unverified_users = OrderedDict()
        self.unverified_users_limit = 1000
        self.unverified_users_max_age = 60 * 5

        self.verified_users = OrderedDict()
        self.verified_users_limit = 20
        self.verified_users_max_age = 60 * 60 * 24

        self.evict_interval = 60 * 5

    def exists(self, token):
        """Check if the user is already saved in the session.
        """
        return token in self.unverified_users or token in self.verified_users

    def load(self, token):
        """Given a user token, attempt to load the user from the session.
        """
        if token in self.unverified_users:
            self.unverified_users.move_to_end(token)
            return self.unverified_users[token]
        elif token in self.verified_users:
            self.verified_users.move_to_end(token)
            return self.verified_users[token]
        else:
            return None

    def save(self, token, user):
        """Save a user to the session backend.

        If the user limit is reached for the session cache, the oldest active
        user will be evicted to make room.
        """
        if user.verified:
            logger.info(f'Saving user {token} as verified')
            self.unverified_users.pop(token, None)
            self.verified_users[token] = user
            while len(self.verified_users) > self.verified_users_limit:
                token, user = self.verified_users.popitem(last=False)
                logger.info(f'Evicting user {token}')
                user.finish_game()
        else:
            logger.info(f'Saving user {token} as unverified')
            self.verified_users.pop(token, None)
            self.unverified_users[token] = user
            while len(self.unverified_users) > self.unverified_users_limit:
                token, user = self.unverified_users.popitem(last=False)
                logger.info(f'Evicting user {token}')
                user.finish_game()

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
            for token, user in list(self.unverified_users.items()):
                delta = now - user.last_access
                if delta > self.unverified_users_max_age:
                    logger.info(f'Evicting user {token}, delta {delta:.2f}s')
                    self.unverified_users.pop(token, None)
                    user.finish_game()
                else:
                    break

            count = len(self.verified_users)
            logger.info(f'Total {count} verified users')
            for token, user in list(self.verified_users.items()):
                delta = now - user.last_access
                if delta > self.verified_users_max_age:
                    logger.info(f'Evicting user {token}, delta {delta:.2f}s')
                    self.verified_users.pop(token, None)
                    user.finish_game()
                else:
                    break


class User:
    """Interface around stateful user interactions.
    """

    # Global session state shared among all threads
    session = Session()

    def __init__(self, token):
        self.token = token
        self.verified = False
        self.game = None
        self.last_access = None

        self._captcha_question = None
        self._captcha_answer = None

    @property
    def persistent(self):
        """Is the user's state saved in the session backend.
        """
        return self.session.exists(self.token)

    def save(self):
        """Save the user to the session backend.
        """
        self.session.save(self.token, self)

    @classmethod
    def load(cls, token):
        """Attempt to load the user from the session backend.

        Will create a new user if the token doesn't exist.
        """
        user = cls.session.load(token)
        if not user:
            user = cls(token)

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

    def communicate(self, text):
        return self.game.communicate(text)

    def start_game(self, game):
        logger.info(f'Starting Frotz for user {self.token}, game {game}')
        self.finish_game()
        self.game = FrotzProcess(game)

    def finish_game(self):
        if self.game:
            name = self.game.game_name
            logger.info(f'Stopping Frotz for user {self.token}, game {name}')
            self.game.close()
            self.game = None


@app.before_request
def load_user():
    token = request.environ['REMOTE_ADDR']
    g.user = User.load(token)


@app.context_processor
def add_context():
    # A random nonce to prevent clients from caching pages
    nonce = secrets.token_urlsafe(8)
    token = g.user.token
    try:
        game = g.user.game.game_name
    except AttributeError:
        game = None

    return {'nonce': nonce, 'current_game': game, 'token': token}


@app.route('/newgame/<game>')
@app.route('/newgame/<game>/<nonce>')
@app.route('/newgame/<game>/<action>/<nonce>')
def new_game(game, action=None, nonce=None):
    if game not in FrotzProcess.games:
        message = "Whoops! It looks like you're trying to access an invalid URL."
        return gopher.render_menu_template('error.gopher', message=message)

    user = g.user
    if not user.verified:
        user.check_captcha(request.environ['SEARCH_TEXT'])
        if not user.verified:
            captcha = user.get_captcha()
            is_invalid = bool(request.environ['SEARCH_TEXT'])
            return gopher.render_menu_template(
                'new_game.gopher',
                game=game,
                captcha=captcha,
                is_invalid=is_invalid,
            )

    if user.game and action != 'confirm':
        confirmed = False
    else:
        confirmed = True
        user.start_game(game)

    return gopher.render_menu_template('new_game.gopher', game=game, confirmed=confirmed)


@app.route('/game')
@app.route('/game/<nonce>')
@app.route('/game/<action>/<nonce>')
def play_game(action=None, nonce=None):
    user = g.user
    if not user.game:
        message = "Whoops! It looks like you're trying to access an invalid URL."
        return gopher.render_menu_template('error.gopher', message=message)

    if action == 'return':
        command = ''
    else:
        command = request.environ['SEARCH_TEXT'] or None

    try:
        screen = user.communicate(command)
    except GameEnded:
        user.finish_game()
        message = 'Your game has ended.'
        return gopher.render_menu_template('error.gopher', message=message)

    screen, _, prompt = screen.rpartition('\n')
    return gopher.render_menu_template('play_game.gopher', screen=screen, prompt=prompt)


@app.route('/lost_pig')
def lost_pig():
    return gopher.render_menu_template('lost_pig.gopher')


@app.route('/tangle')
def tangle():
    return gopher.render_menu_template('tangle.gopher')


@app.route('/zork')
def zork():
    return gopher.render_menu_template('zork.gopher')


@app.route('/planetfall')
def planetfall():
    return gopher.render_menu_template('planetfall.gopher')


@app.route('/')
@app.route('/index/<nonce>')
def index(nonce=None):
    return gopher.render_menu_template('index.gopher')


if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())

    evict_thread = threading.Thread(target=User.session.evict_forever)
    evict_thread.daemon = True
    evict_thread.start()

    app.run(
        host=app.config['HOST'],
        port=app.config['PORT'],
        threaded=app.config['THREADED'],
        processes=app.config['PROCESSES'],
        request_handler=GopherRequestHandler
    )
