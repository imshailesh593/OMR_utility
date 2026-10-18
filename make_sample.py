"""Generate a sample guest-list PDF (names, numbers, blue D/L marks) to try the app with."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

out = Path("sample"); out.mkdir(exist_ok=True)
W, H = 1700, 2200
f = ImageFont.truetype("arial.ttf", 44); big = ImageFont.truetype("ariali.ttf", 64)
people = [("Asha", "Patel", "9876543210", "D"), ("Ravi", "Kumar", "9123456780", "L"), ("Meera", "Shah", "9988776655", "D"),
          ("John", "Dsouza", "9012345678", ""), ("Sunil", "Deshmukh", "9823012345", "L"), ("Priya", "Nair", "9765432109", "D")]

def page(rows):
    im = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(im)
    d.text((120, 100), "Name", font=f, fill="black"); d.text((520, 100), "Last name", font=f, fill="black"); d.text((960, 100), "Number", font=f, fill="black")
    for i, (a, b, n, m) in enumerate(rows):
        y = 220 + i * 130
        d.text((120, y), a, font=f, fill="black"); d.text((520, y), b, font=f, fill="black"); d.text((960, y), n, font=f, fill="black")
        if m: d.text((1450, y - 12), m, font=big, fill=(25, 45, 190))
    return im

pgs = [page(people[:3]), page(people[3:])]
pgs[0].save(out / "guestlist.pdf", save_all=True, append_images=pgs[1:])
