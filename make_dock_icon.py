"""Generate a unique 'Mindshine Time' icon: sunrise gradient + sun-as-clock."""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 4096  # supersampled canvas, downscaled to 1024 at the end


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


# --- background: vertical dawn gradient inside a macOS squircle-ish rounded rect
stops = [
    (0.00, (24, 18, 66)),    # deep indigo night
    (0.45, (122, 44, 110)),  # violet-magenta
    (0.72, (232, 93, 74)),   # coral
    (1.00, (255, 176, 59)),  # golden dawn
]
grad = Image.new("RGB", (1, S))
for y in range(S):
    t = y / (S - 1)
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t0 <= t <= t1:
            grad.putpixel((0, y), lerp(c0, c1, (t - t0) / (t1 - t0)))
            break
grad = grad.resize((S, S))

# macOS Big Sur icon grid: content square is ~824/1024, corner radius ~185/1024
margin = round(S * (100 / 1024))
radius = round(S * (185 / 1024))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [margin, margin, S - margin, S - margin], radius=radius, fill=255
)

icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(icon)

cx, cy = S // 2, round(S * 0.575)  # sun sits low, like it's rising
sun_r = round(S * 0.185)

# --- soft glow behind the sun
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse(
    [cx - sun_r * 2.1, cy - sun_r * 2.1, cx + sun_r * 2.1, cy + sun_r * 2.1],
    fill=(255, 220, 140, 110),
)
glow = glow.filter(ImageFilter.GaussianBlur(S // 28))
glow.putalpha(Image.composite(glow.getchannel("A"), Image.new("L", (S, S), 0), mask))
icon = Image.alpha_composite(icon, glow)
d = ImageDraw.Draw(icon)

# --- sun rays: 12 rounded spokes, skipping below the horizon-ish bottom
ray_col = (255, 236, 179, 255)
w = round(S * 0.022)
for i in range(12):
    ang = math.radians(i * 30 - 90)
    r0, r1 = sun_r * 1.32, sun_r * 1.62
    # longer rays at the cardinal points for a hand-set look
    if i % 3 == 0:
        r1 = sun_r * 1.78
    x0, y0 = cx + r0 * math.cos(ang), cy + r0 * math.sin(ang)
    x1, y1 = cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)
    d.line([x0, y0, x1, y1], fill=ray_col, width=w)
    for x, y in ((x0, y0), (x1, y1)):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=ray_col)

# --- sun disc (clock face)
d.ellipse(
    [cx - sun_r, cy - sun_r, cx + sun_r, cy + sun_r],
    fill=(255, 246, 219, 255),
    outline=(255, 214, 130, 255),
    width=round(S * 0.008),
)

# --- clock hands at 10:10, deep indigo to echo the top of the gradient
hand = (43, 33, 92, 255)


def draw_hand(angle_deg, length, width):
    ang = math.radians(angle_deg - 90)
    x1, y1 = cx + length * math.cos(ang), cy + length * math.sin(ang)
    d.line([cx, cy, x1, y1], fill=hand, width=width)
    d.ellipse([x1 - width / 2, y1 - width / 2, x1 + width / 2, y1 + width / 2], fill=hand)


draw_hand(305, sun_r * 0.52, round(S * 0.024))  # hour hand -> ~10
draw_hand(60, sun_r * 0.78, round(S * 0.018))   # minute hand -> ~:10
dot = round(S * 0.020)
d.ellipse([cx - dot, cy - dot, cx + dot, cy + dot], fill=hand)

# --- four tick marks on the face
for i in range(4):
    ang = math.radians(i * 90 - 90)
    r0, r1 = sun_r * 0.82, sun_r * 0.92
    x0, y0 = cx + r0 * math.cos(ang), cy + r0 * math.sin(ang)
    x1, y1 = cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)
    d.line([x0, y0, x1, y1], fill=(214, 168, 96, 255), width=round(S * 0.010))

final = icon.resize((1024, 1024), Image.LANCZOS)
final.save("icon_1024.png")
print("wrote icon_1024.png")
