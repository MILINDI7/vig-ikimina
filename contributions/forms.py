import warnings
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from django import forms
from django.core.files.base import ContentFile
from django.utils import timezone

MAX_PROOF_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 20_000_000

class ProofForm(forms.Form):
    image = forms.FileField(label="Proof image", help_text="JPG, PNG or WebP, up to 5 MB and 20 megapixels. Images are re-encoded to strip metadata.", widget=forms.FileInput(attrs={"accept": ".jpg,.jpeg,.png,.webp"}))
    def clean_image(self):
        upload = self.cleaned_data["image"]
        if upload.size > MAX_PROOF_BYTES: raise forms.ValidationError("The image must be no larger than 5 MB.")
        if Path(upload.name).suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            raise forms.ValidationError("Upload a JPG, PNG or WebP image.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(upload)
                if image.format not in ("JPEG", "PNG", "WEBP") or image.width * image.height > MAX_PIXELS:
                    raise forms.ValidationError("Use a JPG, PNG or WebP image no larger than 20 megapixels.")
                image.verify()
                upload.seek(0)
                with Image.open(upload) as source:
                    oriented = ImageOps.exif_transpose(source)
                    rgba = oriented.convert("RGBA")
                    clean = Image.new("RGB", rgba.size, "white")
                    clean.paste(rgba, mask=rgba.getchannel("A"))
                    output = BytesIO()
                    clean.save(output, format="JPEG", quality=88, optimize=True)
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise forms.ValidationError("This file is not a safe, readable image.")
        if output.tell() > MAX_PROOF_BYTES:
            raise forms.ValidationError("The processed image is too large. Use a smaller image.")
        return ContentFile(output.getvalue(), name="proof.jpg")

class ReviewForm(forms.Form):
    submission_id = forms.IntegerField(widget=forms.HiddenInput)
    decision = forms.ChoiceField(choices=[("approved", "Approve"), ("declined", "Decline")])
    reason = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))
    def clean(self):
        data = super().clean()
        if data.get("decision") == "declined" and not data.get("reason", "").strip():
            self.add_error("reason", "A reason is required when declining proof.")
        return data

class PeriodForm(forms.Form):
    month = forms.DateField(input_formats=["%Y-%m"], widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}), help_text="Creates obligations for eligible current memberships, including any missing contributions. Existing records are preserved.")
    def clean_month(self):
        month = self.cleaned_data["month"].replace(day=1)
        if month > timezone.localdate().replace(day=1): raise forms.ValidationError("Future months cannot be opened yet.")
        return month
