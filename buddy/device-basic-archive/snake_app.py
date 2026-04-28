"""Snake, adapted for M5Stack's three physical buttons.

Controls:
  A = left, C = right, B = flip vertical.

B is "up if already going down, and vice versa" — from a horizontal
heading it picks the opposite of the last vertical direction the
snake traveled, so the first B press from the starting rightward
run lands on UP. From a vertical heading, B is a 180 flip; that
will self-collide when the snake is longer than 1 cell, which is
an intentional consequence of the control spec.

Playfield is a 20x12 grid of 16px cells filling the area below the
header. Only cells that changed are repainted each tick — redrawing
the whole field causes visible flicker on the ILI9342.

Styling follows the shared Anthropic palette: the snake is orange,
food is cream, and the game-over idle screen runs the same starburst
animation used on the launcher.
"""

import random
import time

import M5

import ui_theme


_CELL = 16
_GRID_W = 20        # 320 / 16
_GRID_H = 12        # 192 / 16
_PLAY_X = 0
_PLAY_Y = 24        # sits below the 22 px header + hairline

_BLACK = ui_theme.BLACK
_SNAKE = ui_theme.ORANGE
_FOOD = ui_theme.CREAM
_CHROME_TEXT = ui_theme.CREAM
_DIVIDER = ui_theme.ORANGE_DIM

_LEFT = (-1, 0)
_RIGHT = (1, 0)
_UP = (0, -1)
_DOWN = (0, 1)

# Input is polled every 40 ms; the snake advances every _MOVE_TICKS
# polls. 4 * 40 ms = 160 ms per step is a comfortable classic pace.
_MOVE_TICKS = 4


def _draw_cell(cx, cy, color):
    M5.Lcd.fillRect(_PLAY_X + cx * _CELL, _PLAY_Y + cy * _CELL, _CELL, _CELL, color)


def _draw_chrome(score):
    lcd = M5.Lcd
    lcd.fillScreen(_BLACK)
    ui_theme.header(lcd, "Snake")
    _update_score(score)
    ui_theme.footer(lcd, "A: left", "B: up/dn", "C: right")


def _update_score(score):
    lcd = M5.Lcd
    # Keep the orange hairline at y=22 intact — only overwrite the
    # dark portion of the header strip.
    lcd.fillRect(200, 0, 120, 22, ui_theme.DARK)
    lcd.setTextColor(_CHROME_TEXT, ui_theme.DARK)
    lcd.setCursor(210, 7)
    lcd.print("score: {}".format(score))


def _random_food(snake):
    occupied = set(snake)
    # Grid has 240 cells so even near-full snakes spin only briefly.
    while True:
        cell = (random.randint(0, _GRID_W - 1), random.randint(0, _GRID_H - 1))
        if cell not in occupied:
            return cell


def _game_over(score):
    """Idle end-of-round screen with a pulsing burst behind the score."""
    lcd = M5.Lcd
    # Redraw header + footer for this state; the body is fully refreshed.
    ui_theme.header(lcd, "Snake   /   Game over")
    ui_theme.clear_body(lcd)
    ui_theme.footer(lcd, "A: again", "", "C: menu")

    try:
        lcd.setTextSize(3)
    except Exception:
        pass
    lcd.setTextColor(ui_theme.RED, _BLACK)
    lcd.setCursor(78, 40)
    lcd.print("Game over")
    try:
        lcd.setTextSize(2)
    except Exception:
        pass
    lcd.setTextColor(ui_theme.CREAM, _BLACK)
    lcd.setCursor(100, 78)
    lcd.print("score: {}".format(score))
    try:
        lcd.setTextSize(1)
    except Exception:
        pass

    # Burst low in the body so it doesn't collide with the text above.
    cx, cy = 160, 160
    ui_theme.draw_burst(cx, cy, 0, max_r=28)
    frame = 1
    last_tick = time.ticks_ms()
    while True:
        M5.update()
        if M5.BtnA.wasPressed():
            return "restart"
        if M5.BtnC.wasPressed():
            return "exit"
        frame, last_tick = ui_theme.tick_burst(cx, cy, frame, last_tick, max_r=28)
        time.sleep_ms(15)


def _play_round():
    head = (_GRID_W // 2, _GRID_H // 2)
    snake = [head, (head[0] - 1, head[1]), (head[0] - 2, head[1])]
    direction = _RIGHT
    # last_vertical seeds B's flip rule. Starting at DOWN means the
    # first B press from the initial rightward run sends the snake UP.
    last_vertical = _DOWN
    pending_dir = direction
    score = 0

    _draw_chrome(score)
    for cell in snake:
        _draw_cell(cell[0], cell[1], _SNAKE)
    food = _random_food(snake)
    _draw_cell(food[0], food[1], _FOOD)

    tick = 0
    while True:
        M5.update()

        # One input wins per step — we latch into pending_dir and
        # apply at the next move tick so a rapid A-then-C can't queue
        # a 180 through an intermediate direction.
        if M5.BtnA.wasPressed():
            if direction != _RIGHT:
                pending_dir = _LEFT
        if M5.BtnC.wasPressed():
            if direction != _LEFT:
                pending_dir = _RIGHT
        if M5.BtnB.wasPressed():
            pending_dir = _UP if last_vertical == _DOWN else _DOWN

        tick += 1
        if tick < _MOVE_TICKS:
            time.sleep_ms(40)
            continue
        tick = 0

        direction = pending_dir
        if direction == _UP or direction == _DOWN:
            last_vertical = direction

        new_head = (snake[0][0] + direction[0], snake[0][1] + direction[1])

        if (new_head[0] < 0 or new_head[0] >= _GRID_W
                or new_head[1] < 0 or new_head[1] >= _GRID_H):
            return score

        # Tail cell is about to vacate unless we're eating, so it's
        # not a collider in the normal case.
        ate = new_head == food
        body = snake if ate else snake[:-1]
        if new_head in body:
            return score

        snake.insert(0, new_head)
        _draw_cell(new_head[0], new_head[1], _SNAKE)

        if ate:
            score += 1
            _update_score(score)
            food = _random_food(snake)
            _draw_cell(food[0], food[1], _FOOD)
        else:
            tail = snake.pop()
            _draw_cell(tail[0], tail[1], _BLACK)

        time.sleep_ms(40)


def run():
    while True:
        score = _play_round()
        if _game_over(score) == "exit":
            return
