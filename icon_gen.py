"""Gera o ícone oficial do ReelForge (.ico) e cria o atalho na Área de Trabalho."""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont


def ensure_icon(root):
    dest = Path(root) / 'assets' / 'reelforge.ico'
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    # Fundo arredondado escuro com gradiente Sasuke (azul/roxo) -> Naruto (laranja/dourado).
    bg = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    pix = bg.load()
    for y in range(size):
        for x in range(size):
            t = (x * 0.65 + y * 0.35) / size
            r = int(18 * (1 - t) + 36 * t)
            g = int(20 * (1 - t) + 22 * t)
            b = int(38 * (1 - t) + 18 * t)
            pix[x, y] = (r, g, b, 255)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((8, 8, size - 9, size - 9), radius=56, fill=255)
    img.paste(bg, (0, 0), mask)

    # Brilho duplo (azul à esquerda, laranja à direita)
    glow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((18, 28, 150, 228), fill=(56, 130, 246, 95))
    gdraw.ellipse((106, 28, 238, 228), fill=(255, 122, 24, 110))
    glow = glow.filter(ImageFilter.GaussianBlur(24))
    img = Image.alpha_composite(img, Image.composite(glow, Image.new('RGBA', (size, size), (0, 0, 0, 0)), mask))

    draw = ImageDraw.Draw(img)
    # Moldura de Reel vertical (9:16) centralizada
    rx0, ry0, rx1, ry1 = 68, 28, 188, 228
    draw.rounded_rectangle((rx0 - 3, ry0 - 3, rx1 + 3, ry1 + 3), radius=26, outline=(255, 168, 38, 235), width=5)
    draw.rounded_rectangle((rx0, ry0, rx1, ry1), radius=24, fill=(14, 17, 26, 230), outline=(79, 140, 255, 220), width=3)

    # Perfurações laterais de filme estilizadas
    for ky in range(ry0 + 22, ry1 - 18, 34):
        draw.rounded_rectangle((rx0 + 10, ky, rx0 + 20, ky + 16), radius=4, fill=(96, 165, 250, 210))
        draw.rounded_rectangle((rx1 - 20, ky, rx1 - 10, ky + 16), radius=4, fill=(251, 146, 60, 220))

    # Triângulo de Play central com sombra e destaque amarelo
    shadow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    pts = [(108, 92), (108, 164), (164, 128)]
    sdraw.polygon([(x, y + 4) for x, y in pts], fill=(0, 0, 0, 180))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(5)))
    draw = ImageDraw.Draw(img)
    draw.polygon(pts, fill=(255, 212, 38, 255), outline=(255, 255, 255, 240))

    # Borda externa do ícone
    draw.rounded_rectangle((8, 8, size - 9, size - 9), radius=56, outline=(255, 150, 40, 200), width=4)

    img.save(dest, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return dest


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    print(ensure_icon(root))
