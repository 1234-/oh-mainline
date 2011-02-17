# This file is part of OpenHatch.
# Copyright (C) 2010 Parker Phinney
# Copyright (C) 2009, 2010 OpenHatch, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import django.contrib.auth.forms
from django.contrib.auth.models import User
import django.forms
from mysite.profile.models import Person
import StringIO
from django.core.files.images import get_image_dimensions
from django.core.files.uploadedfile import InMemoryUploadedFile
import PIL.Image
from django.conf import settings
from invitation.models import InvitationKey
from models import InvitationRequest
import django_authopenid.forms

RESERVED_USERNAMES =  (
        'admin',
        'anonymous',
        'sufjan',
        'Spam cleanup script',
        )

class UserCreationFormWithEmail(django.contrib.auth.forms.UserCreationForm):
    username = django.forms.RegexField(label="Username", max_length=30, regex=r'^\w+$',
        help_text = "<span class='help_text'>Pick a froopy handle, with length < 31 characters. Stick to letters, digits and underscores.</span>",
        error_messages = {'invalid': "Stick to letters, digits and underscores.", 'required': "Gotta pick a username!"})
    email = django.forms.EmailField(error_messages={
        'required': "Your email address is required. We promise to use it respectfully.",
        'invalid': "This email address looks fishy. Real, or malarkey?"})
    class Meta:
        model = django.contrib.auth.models.User
        fields = ('username', 'email', 'password1')

    def __init__(self, *args, **kw):
        super(django.contrib.auth.forms.UserCreationForm,
                self).__init__(*args, **kw)

        custom_error_messages = {}
        custom_error_messages_dict = {
                "A user with that username already exists.": "Oops, we've already got a user in our database with that username. Pick another one!",
                "A user with that email already exists.": "We've already got a user in our database with that email address. Have you signed up before?",
                }

        for fieldname in self.errors:
            for index, error_text in enumerate(self.errors[fieldname]):
                uet = unicode(error_text)
                self.errors[fieldname][index] = custom_error_messages_dict.get(uet, uet)

    def clean_username(self):
        username = super(UserCreationFormWithEmail, self).clean_username()
        if username not in RESERVED_USERNAMES:
            return username
        raise django.forms.ValidationError(
                "That username is reserved.")

    def clean_email(self):
        """Verify that their email is unique."""
        email = self.cleaned_data["email"]
        try:
            User.objects.get(email=email)
        except User.DoesNotExist:
            return email
        raise django.forms.ValidationError(
            "A user with that email already exists.")

class ShowEmailForm(django.forms.Form):
    show_email = django.forms.BooleanField(required=False, label="Make email publicly visible?")

class EditEmailForm(django.forms.ModelForm):
    class Meta:
        model = django.contrib.auth.models.User
        fields = ('email',)

    def clean_email(self):
        """Verify that their email is unique."""
        email = self.cleaned_data["email"]
        other_users_with_this_email = list(
                User.objects.filter(email=email).exclude(
                    username=self.instance.username))
        if not other_users_with_this_email:
            return email
        else:
            raise django.forms.ValidationError(
                "A user with that email already exists.")

class EditLocationForm(django.forms.ModelForm):
    class Meta:
        model = Person
        fields = ('location_display_name',)
    def clean_location_display_name(self):
        return self.cleaned_data['location_display_name'].strip()

class EditNameForm(django.forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name')

class EditPhotoForm(django.forms.ModelForm):
    class Meta:
        model = Person
        fields = ('photo',)
    
    def clean_photo(self):
        # Safe copy of data...
        self.cleaned_data['photo'].seek(0)
        data = self.cleaned_data['photo'].read()
        self.cleaned_data['photo'].seek(0)
        data_fd = StringIO.StringIO(data)

        w, h = get_image_dimensions(data_fd)
        if w > 200:
            # Scale it down.
            too_big = PIL.Image.open(StringIO.StringIO(data))
            format = too_big.format
            new_w = 200
            new_h = (h * 1.0 / w) * 200

            smaller = too_big.resize((new_w, new_h),
                                     PIL.Image.ANTIALIAS)

            # "Save" it to memory
            new_image_fd = StringIO.StringIO()
            smaller.save(new_image_fd, format=format)
            new_image_fd.seek(0)

            old = self.cleaned_data['photo']

            new_image_uploaded_file = InMemoryUploadedFile(
                new_image_fd, '', old.name,
                old.content_type, new_image_fd.len, old.charset)

            # Modify the self.cleaned_data[]
            self.cleaned_data['photo'] = new_image_uploaded_file
        return self.cleaned_data['photo']

class SignUpIfYouWantToHelpForm(django.forms.Form):
    how_should_people_contact_you = django.forms.ChoiceField(
            initial='forwarder',
            widget=django.forms.RadioSelect,
            label="You've expressed interest in helping out a project. How can people from that project contact you?",
            choices=(
                ('forwarder', 'By email, but mask my email address using an automatic forwarder (like Craigslist)'),
                ('public_email', 'By email; just display my real email address'),
                ))

class EmailMeForm(django.forms.ModelForm):
    class Meta:
        model = Person
        fields = ('email_me_weekly_re_projects',)
