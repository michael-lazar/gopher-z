#!/usr/bin/env python3
import logging
import secrets
import threading

from flask import Flask, g, request
from flask_gopher import GopherExtension, GopherRequestHandler

from gopherz.frotz import Frotz, GameEnded
from gopherz.session import User

logger = logging.getLogger(__name__)

app = Flask(__name__, static_url_path="")
app.config.from_pyfile("gopher-z.cfg")

gopher = GopherExtension(app)


def render_error_page(message=None):
    if not message:
        message = "Whoops! It looks like you're trying to access an invalid URL."
    return gopher.render_menu_template("error.gopher", message=message)


@app.before_request
def load_user():
    pk = request.environ["REMOTE_ADDR"]
    g.user = User.load(pk)


@app.context_processor
def add_context():
    # A random nonce added to URLs for pages with dynamic content.
    # This prevents gopher clients (namely lynx) from caching pages.
    return {"nonce": secrets.token_urlsafe(8), "current_game": g.user.game}


@app.route("/newgame/<game>")
@app.route("/newgame/<game>/<nonce>")
@app.route("/newgame/<game>/<action>/<nonce>")
def new_game(game, action=None, nonce=None):
    if game not in Frotz.GAMES:
        return render_error_page()

    captcha_answer = request.environ["SEARCH_TEXT"]

    user = g.user
    if not user.verified:
        user.check_captcha(captcha_answer)
        if not user.verified:
            captcha = user.get_captcha()
            is_invalid = bool(captcha_answer)
            context = {"game": game, "captcha": captcha, "is_invalid": is_invalid}
            return gopher.render_menu_template("new_game.gopher", **context)

    # Show a confirmation screen if the user already has a game in-progress
    if user.frotz and action != "confirm":
        confirmed = False
    else:
        confirmed = True
        if user.frotz:
            user.frotz.close()
        user.frotz = Frotz(game)

    context = {"game": game, "confirmed": confirmed}
    return gopher.render_menu_template("new_game.gopher", **context)


@app.route("/game")
@app.route("/game/<nonce>")
@app.route("/game/<action>/<nonce>")
def play_game(action=None, nonce=None):
    user = g.user
    if not user.frotz:
        return render_error_page()

    if action == "return":
        command = ""
    else:
        command = request.environ["SEARCH_TEXT"] or None

    try:
        screen = user.frotz.communicate(command)
    except GameEnded:
        if user.frotz:
            user.frotz.close()
            user.frotz = None
        return render_error_page("Your game has ended.")

    screen, _, prompt = screen.rpartition("\n")
    context = {"screen": screen, "prompt": prompt}
    return gopher.render_menu_template("play_game.gopher", **context)


@app.route("/lost_pig")
def lost_pig():
    return gopher.render_menu_template("lost_pig.gopher")


@app.route("/tangle")
def tangle():
    return gopher.render_menu_template("tangle.gopher")


@app.route("/zork")
def zork():
    return gopher.render_menu_template("zork.gopher")


@app.route("/planetfall")
def planetfall():
    return gopher.render_menu_template("planetfall.gopher")


@app.route("/")
@app.route("/index/<nonce>")
def index(nonce=None):
    return gopher.render_menu_template("index.gopher")


if __name__ == "__main__":
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())

    evict_thread = threading.Thread(target=User.session.evict_forever)
    evict_thread.daemon = True
    evict_thread.start()

    app.run(
        host=app.config["HOST"],
        port=app.config["PORT"],
        threaded=True,
        request_handler=GopherRequestHandler,
    )
