import os
import select
import logging
import subprocess
from string import ascii_letters, digits

logger = logging.getLogger(__name__)

GAME_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), '..', 'games')


class GameEnded(Exception):
    """The frotz process no longer exists.
    """


class Frotz:
    """Wrapper around communication with the frotz command line program.

    Frotz is launched as a long-running subprocess. The user communicates
    with the process by sending text commands that end with a newline. The
    response fromt is immediately printed to stdout.
    """
    COMMAND = ['dfrotz', '-p', '-w 67', '-S 67']
    GAMES = {
        'tangle': 'Tangle.z5',
        'lost-pig': 'LostPig.z8',
        'zork': 'ZORK1.DAT',
        'planetfall': 'PLANETFA.DAT'
    }

    def __init__(self, game):
        self.game = game
        self.game_file = self.GAMES[game]
        self.process = None
        self.last_screen = ''

    def launch(self):
        """Launch the subprocess and load the initial frotz game screen.
        """
        data_path = os.path.join(GAME_DIR, self.game_file)
        self.process = subprocess.Popen(
            self.COMMAND + [data_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0
        )
        logger.info(f'Launched process {self.process.pid}')
        return self._get_screen()

    def communicate(self, command):
        """Forward a command from the user to the running process.

        The command will be strictly sanitized to prevent injection attacks.

        A GameEnded exception will be raised if the process is no longer
        responding. This could happen if the OS kills the PID.
        """
        try:
            return self._communicate(command)
        except OSError:
            logger.exception(f'Communication error with {self.process.pid}')
            raise GameEnded()

    def close(self):
        """Attempt to close the process if it still exists.
        """
        if self.process:
            logger.info(f'Attempting to kill process {self.process.pid}')
            try:
                self.process.kill()
            except OSError:
                logger.exception(f'Error killing process {self.process.pid}')
            self.process = None
        self.last_screen = ''

    def _communicate(self, command):
        if not self.process:
            return self.launch()

        if command is None:
            return self.last_screen

        command = self._sanitize(command)
        for cmd in ('RESTORE', 'SAVE', 'SCRIPT', 'UNSCRIPT'):
            if command.startswith(cmd):
                return (f"I'm sorry, but the \"{cmd}\" command is not "
                        f"currently implemented\n\n>")

        command += '\n'
        self.process.stdin.write(command.encode())
        self.process.stdin.flush()
        return self._get_screen()

    def _get_screen(self):
        lines = []
        while select.select([self.process.stdout], [], [], 0.1)[0]:
            data = self.process.stdout.read(2048)
            if not data:
                logger.error(f'Process stopped responding {self.process.pid}')
                raise GameEnded()
            lines.append(data)

        screen = b''.join(lines).decode()
        self.last_screen = screen
        return screen

    def _sanitize(self, command):
        # Be over-conservative about what characters are allowed in commands
        safe_characters = ascii_letters + digits + '.,?! '

        command = command[:128]
        command = ''.join(c for c in command if c in safe_characters)
        command = command.strip('.,?! ').upper()
        return command
