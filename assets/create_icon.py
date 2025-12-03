from PIL import Image

# Open a PNG/JPG image
img = Image.open("icon.png")
# Save as ICO (include multiple sizes for best results)
img.save("app.ico", format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])