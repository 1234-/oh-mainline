# This file is part of OpenHatch.
# Copyright (C) 2010, 2011 OpenHatch, Inc.
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

import mysite.base.unicode_sanity
import lxml.html
import urllib
import urllib2
import cStringIO as StringIO
import re
import simplejson
import datetime
import collections
import logging
import urlparse
import hashlib
import xml.etree.ElementTree as ET
import xml.parsers.expat

from django.utils.encoding import force_unicode

import mysite.search.models
import mysite.profile.models
import mysite.base.helpers

import twisted.web

### Generic error handler
class ProfileImporter(object):
    SQUASH_THESE_HTTP_CODES = []

    def squashIrrelevantErrors(self, error):
        squash_it = False

        if error.type == twisted.web.error.Error:
            if error.value.status in self.SQUASH_THESE_HTTP_CODES:
                # The username doesn't exist. That's okay. It just means we have gleaned no profile information
                # from this query.
                squash_it = True

            if squash_it:
                pass
            else:
                # This is low-quality logging for now!
                logging.warn("EEK: " + error.value.status + " " + error.value.response)
        else:
            raise error.value

    def __init__(self, query, dia_id, command):
        ## First, store the data we are passed in.
        self.query = query
        self.dia_id = dia_id
        self.command = command
        ## Then, create a mapping for storing the URLs we are waiting on.
        self.urls_we_are_waiting_on = collections.defaultdict(int)

    def get_dia(self):
        """We have this method so to avoid holding on to any objects from
        the database for a long time.

        Event handler methods should use a fresh database object while they
        run, rather than using some old object that got created when the class
        was instantiated.
        """
        return mysite.profile.models.DataImportAttempt.objects.get(
            id=self.dia_id)

    def markThatTheDeferredFinished(self, url):
        self.urls_we_are_waiting_on[url] -= 1
        # If we just made the state totally insane, then log a warning to that effect.
        if self.urls_we_are_waiting_on[url] < 0:
            logging.error("Eeek, " + url + " went negative.")
        if self.seems_finished():
            # Grab the DataImportAttempt object, and mark it as completed.
            dia = self.get_dia()
            dia.completed = True
            dia.save()
        # Finally, if there is more work to do, enqueue it.
        self.command.create_tasks_from_dias(max=1)

    def seems_finished(self):
        if sum(self.urls_we_are_waiting_on.values()) == 0:
            return True
        return False

    def handleError(self, failure):
        # FIXME: Use Django default exception logic to make an email get sent.
        import logging
        logging.warn(failure)

### This section imports projects from github.com
class ImportActionWrapper(object):
    # This class serves to hold three things:
    # * the URL we requested, and
    # * the ProfileImporter object that caused the URL to be requested.
    # * Function to call.
    #
    # The point of this wrapper is that we call that function for you, and
    # afterward, we call .markThatTheDeferredFinished() on the ProfileImporter.
    #
    # That way, the ProfileImporter can update its records of which URLs have finished
    # being processed.
    def __init__(self, url, pi, fn):
        self.url = url
        self.pi = pi
        self.fn = fn

    def __call__(self, *args, **kwargs):
        # Okay, so we call fn and pass in arguments to it.
        value = self.fn(*args, **kwargs)

        # Then we tell the ProfileImporter that we have handled the URL.
        self.pi.markThatTheDeferredFinished(self.url)
        return value

class GithubImporter(ProfileImporter):

    def squashIrrelevantErrors(self, error):
        squash_it = False

        if error.type == twisted.web.error.Error:
            if error.value.status == '404':
                # The username doesn't exist. That's okay. It just means we have gleaned no profile information
                # from this query.
                squash_it = True
            if error.value.status == '401' and error.value.response == '{"error":"api route not recognized"}':
                # This is what we get when we query e.g. http://github.com/api/v2/json/repos/show/asheesh%40asheesh.org
                # It just means that Github decided that asheesh@asheesh.org is not a valid username.
                # Just like above -- no data to return.
                squash_it = True

            if squash_it:
                pass
            else:
                # This is low-quality logging for now!
                logging.warn("EEK: " + error.value.status + " " + error.value.response)
        else:
            raise error.value

    # This method takes a repository dict as returned by Github
    # and creates a Citation, also creating the relevant
    # PortfolioEntry if necessary.
    def addCitationFromRepoDict(self, repo_dict, override_contrib=None):
        # Get the DIA whose ID we stored
        dia = self.get_dia()
        person = dia.person

        # Get or create a project by this name
        (project, _) = mysite.search.models.Project.objects.get_or_create(
            name=repo_dict['name'])

        # Look and see if we have a PortfolioEntry. If not, create
        # one.
        if mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project).count() == 0:
            portfolio_entry = mysite.profile.models.PortfolioEntry(person=person,
                                             project=project,
                                             project_description=repo_dict['description'] or '')
            portfolio_entry.save()

        # Either way, it is now safe to get it.
        portfolio_entry = mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project)[0]

        citation = mysite.profile.models.Citation()
        citation.languages = "" # FIXME ", ".join(result['languages'])

        # Fill out the "contributor role", either by data we got
        # from the network, or by special arguments to this
        # function.
        if repo_dict['fork']:
            citation.contributor_role = 'Forked'
        else:
            citation.contributor_role = 'Started'
        if override_contrib:
            citation.contributor_role = override_contrib
        citation.portfolio_entry = portfolio_entry
        citation.data_import_attempt = dia
        citation.url = 'http://github.com/%s/%s/' % (urllib.quote_plus(repo_dict['owner']),
                                                     urllib.quote_plus(repo_dict['name']))
        citation.save_and_check_for_duplicates()

    def handleUserRepositoryJson(self, json_string):
        data = simplejson.loads(json_string)
        if 'repositories' not in data:
            return

        repos = data['repositories']
        # for every repository, we need to get its primary
        # programming language. FIXME.
        # For now we skip that.
        for repo in repos:
            self.addCitationFromRepoDict(repo)

        person = self.get_dia().person

        person.last_polled = datetime.datetime.now()
        person.save()


    def getUrlsAndCallbacks(self):
        urls_and_callbacks = []

        # Well, one thing we can do is get the repositories the user owns.
        this_one = {'errback': self.squashIrrelevantErrors}
        this_one['url'] = ('http://github.com/api/v2/json/repos/show/' +
            mysite.base.unicode_sanity.quote(self.query))
        this_one['callback'] = self.handleUserRepositoryJson
        urls_and_callbacks.append(this_one)

        # Another is look at the user's activity feed.
        this_one = {'errback': self.squashIrrelevantErrors}
        this_one['url'] = ('http://github.com/%s.json' %
            mysite.base.unicode_sanity.quote(self.query))
        this_one['callback'] = self.handleUserActivityFeedJson
        urls_and_callbacks.append(this_one)

        # Another is look at the watched list for repos the user collaborates on
        # FIXME

        return urls_and_callbacks

    def handleUserActivityFeedJson(self, json_string):
        # first, decode it
        data = simplejson.loads(json_string)

        # create a set that we add URLs to. This way, we can avoid
        # returning duplicate URLs.
        repo_urls_found = set()

        for event in data:
            if 'repository' not in event:
                print 'weird, okay'
                continue
            repo = event['repository']
            # Find "collaborated on..."
            if event['type'] == 'PushEvent':
                if repo['owner'] != self.query:
                    ## In that case, we need to find out if the given user is in the list of collaborators
                    ## for the repository. Normally I would call out to a different URL, but I'm supposed to
                    ## not block.
                    ## FIXME: return a Deferred I guess.
                    continue # skip the event for now
            # Find "forked..."
            elif event['type'] == 'ForkEvent':
                if repo['owner'] != self.query:
                    self.addCitationFromRepoDict(repo, override_contrib='Collaborated on')
            elif event['type'] == 'WatchEvent':
                continue # Skip this event.
            else:
                logging.info("When looking in the Github user feed, I found a Github event of unknown type.")

### This section imports package lists from qa.debian.org

SECTION_NAME_AND_NUMBER_SPLITTER = re.compile(r'(.*?) [(](\d+)[)]$')
# FIXME: Migrate this to UltimateDebianDatabase or DebianDatabaseExport

class DebianQA(ProfileImporter):
    SQUASH_THESE_HTTP_CODES = ['404',]

    def getUrlsAndCallbacks(self):
        if '@' in self.query:
            email_address = self.query
        else:
            email_address = self.query + '@debian.org'

        url = 'http://qa.debian.org/developer.php?' + mysite.base.unicode_sanity.urlencode({
            u'login': unicode(email_address)})
        return [ {
            'url': url,
            'errback': self.squashIrrelevantErrors,
            'callback': self.handlePageContents } ]

    def handlePageContents(self, contents):
        '''contents is a string containing the data the web page contained.
        '''
        file_descriptor_wrapping_contents = StringIO.StringIO(contents)

        parsed = lxml.html.parse(file_descriptor_wrapping_contents).getroot()
        
        package_names = self._package_names_from_parsed_document(parsed)
        self._create_citations_from_package_names(package_names)

    def _package_names_from_parsed_document(self, parsed):
        # for each H3 (Like "main" or "non-free" or "Non-maintainer uploads",
        # grab that H3 to figure out the heading. These h3s have a table right next
        # to them in the DOM.
        package_names = []

        for relevant_table in parsed.cssselect('h3+table'):
            num_added = 0
        
            h3 = relevant_table.getprevious()
            table = relevant_table

            h3_text = h3.text_content()
            # this looks something like "main (5)"
            section, number_of_packages = SECTION_NAME_AND_NUMBER_SPLITTER.match(h3_text).groups()

            # Trim trailing whitespace
            section = section.strip()

            # If the section is "Non-maintainer uploads", skip it for now.
            # That's because, for now, this importer is interested only in
            # what packages the person maintains.
            if section == 'Non-maintainer uploads':
                continue

            for package_bold_name in table.cssselect('tr b'):
                package_name = package_bold_name.text_content()
                package_description = package_bold_name.cssselect('span')[0].attrib['title']
                num_added += 1
                package_names.append( (package_name, package_description) )

            assert num_added == int(number_of_packages)

        return package_names

    def _create_citations_from_package_names(self, package_names):
        dia = mysite.profile.models.DataImportAttempt.objects.get(id=self.dia_id)
        person = dia.person

        for package_name, package_description in package_names:
            (project, _) = mysite.search.models.Project.objects.get_or_create(name=package_name)

            package_link = 'http://packages.debian.org/src:' + urllib.quote(
                package_name)

            if mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project).count() == 0:
                portfolio_entry = mysite.profile.models.PortfolioEntry(person=person,
                                                 project=project,
                                                 project_description=package_description)
                portfolio_entry.save()
            portfolio_entry = mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project)[0]
    
            citation = mysite.profile.models.Citation()
            citation.languages = "" # FIXME ", ".join(result['languages'])
            citation.contributor_role='Maintainer'
            citation.portfolio_entry = portfolio_entry
            citation.data_import_attempt = dia
            citation.url = package_link
            citation.save_and_check_for_duplicates()

            # And add a citation to the Debian portfolio entry
            (project, _) = mysite.search.models.Project.objects.get_or_create(name='Debian GNU/Linux')
            if mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project).count() == 0:
                portfolio_entry = mysite.profile.models.PortfolioEntry(person=person,
                                                 project=project,
                                                 project_description=
                                                 'The universal operating system')
                portfolio_entry.save()
            portfolio_entry = mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project)[0]
            citation = mysite.profile.models.Citation()
            citation.languages = '' # FIXME: ?
            citation.contributor_role='Maintainer of %s' % package_name
            citation.portfolio_entry = portfolio_entry
            citation.data_import_attempt = dia
            citation.url = package_link
            citation.save_and_check_for_duplicates()

        person.last_polled = datetime.datetime.now()
        person.save()

class LaunchpadProfilePageScraper(ProfileImporter):
    SQUASH_THESE_HTTP_CODES = ['404',]

    def getUrlsAndCallbacks(self):
        # If the query has an '@' in it, enqueue a task to
        # find the username.
        if '@' in self.query:
            return [self.getUrlAndCallbackForEmailLookup()]
        else:
            return [self.getUrlAndCallbackForProfilePage()]

    def getUrlAndCallbackForEmailLookup(self, query=None):
        if query is None:
            query = self.query

        this_one = {}
        this_one['url'] = ('https://api.launchpad.net/1.0/people?' +
                           'ws.op=find&text=' +
                           mysite.base.unicode_sanity.quote(
                               query))
        this_one['callback'] = self.parseAndProcessUserSearch
        this_one['errback'] = self.squashIrrelevantErrors
        return this_one

    def getUrlAndCallbackForProfilePage(self, query=None):
        if query is None:
            query = self.query
        # Enqueue a task to actually get the user page
        this_one = {}
        this_one['url'] = ('https://launchpad.net/~' +
                           mysite.base.unicode_sanity.quote(query))
        this_one['callback'] = self.parseAndProcessProfilePage
        this_one['errback'] = self.squashIrrelevantErrors
        return this_one
    
    def parseAndProcessProfilePage(self, profile_html):
        PROJECT_NAME_FIXUPS = {
            'Launchpad itself': 'Launchpad',
            'Debian': 'Debian GNU/Linux'}

        doc_u = unicode(profile_html, 'utf-8')
        tree = lxml.html.document_fromstring(doc_u)
        
        contributions = {}
        # Expecting html like this:
        # <table class='contributions'>
        #   <tr>
        #       ...
        #       <img title='Bug Management' />
        #
        # It generates a list of dictionaries like this:
## {
##         'F-Spot': {
##             'url': 'http://launchpad.net/f-spot',
##             'involvement_types': ['Bug Management', 'Bazaar Branches'],
##             'languages' : ['python', 'shell script']
##         }
##        }
        # Extract Launchpad username from page
        if not tree.cssselect('#launchpad-id dd'):
            return # Well, there's no launchpad ID here, so that's that.
        username = tree.cssselect('#launchpad-id dd')[0].text_content().strip()
        for row in tree.cssselect('.contributions tr'):
            project_link = row.cssselect('a')[0]
            project_name = project_link.text_content().strip()
            # FIXUPs: Launchpad uses some weird project names:
            project_name = PROJECT_NAME_FIXUPS.get(project_name,
                                                   project_name)

            project_url_relative = project_link.attrib['href']
            project_url = urlparse.urljoin('https://launchpad.net/',
                                           project_url_relative)
        
            involvement_types = [
                i.attrib.get('title', '').strip()
                for i in row.cssselect('img')]
            contributions[project_name] = {
                'involvement_types': set([k for k in involvement_types if k]),
                'url': project_url,
                'citation_url': "https://launchpad.net/~" + username,
                }

        # Now create Citations for those facts
        for project_name in contributions:
            self._save_parsed_launchpad_data_in_database(
                project_name, contributions[project_name])

    def _save_parsed_launchpad_data_in_database(self, project_name, result):
        dia = self.get_dia()
        person = dia.person
        
        for involvement_type in result['involvement_types']:

            (project, _) = mysite.search.models.Project.objects.get_or_create(name=project_name)

            # This works like a 'get_first_or_create'.
            # Sometimes there are more than one existing PortfolioEntry
            # with the details in question.
            # FIXME: This is untested.
            if mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project).count() == 0:
                portfolio_entry = mysite.profile.models.PortfolioEntry(person=person, project=project)
                portfolio_entry.save()
            portfolio_entry = mysite.profile.models.PortfolioEntry.objects.filter(person=person, project=project)[0]

            citation = mysite.profile.models.Citation()
            citation.contributor_role = involvement_type
            citation.portfolio_entry = portfolio_entry
            citation.data_import_attempt = dia
            citation.url = result['citation_url']
            citation.save_and_check_for_duplicates()

    def parseAndProcessUserSearch(self, user_search_json):
        data = simplejson.loads(user_search_json)
        if data['total_size']:
            entry = data['entries'][0]
        else:
            # No matches. How sad.
            return

        username = entry['name']
        # Now enqueue a task to do the real work.
        self.command.call_getPage_on_data_dict(self,
            self.getUrlAndCallbackForProfilePage(query=username))

class BitbucketImporter(ProfileImporter):
    ROOT_URL = 'http://api.bitbucket.org/1.0/'
    SQUASH_THESE_HTTP_CODES = ['404',]

    def getUrlsAndCallbacks(self):
        return [{
            'errback': self.squashIrrelevantErrors,
            'url': self.url_for_query(self.query),
            'callback': self.processUserJson,
            }]

    def url_for_query(self, query):
        url = self.ROOT_URL
        url += 'users/%s/' % (mysite.base.unicode_sanity.quote(self.query))
        return url

    def url_for_project(self, user_name, project_name):
        return 'http://bitbucket.org/%s/%s/' % (
            mysite.base.unicode_sanity.quote(user_name),
            mysite.base.unicode_sanity.quote(project_name))

    def processUserJson(self, json_string):
        person = self.get_dia().person

        json_data = simplejson.loads(json_string)

        bitbucket_username = json_data['user']['username']
        repositories = json_data['repositories']
        ### The repositories list contains a sequence of dictionaries.
        ### The keys are:
        # slug: The url slug for the project
        # name: The name of the project
        # website: The website associated wit the project, defined by the user
        # followers_count: Number of followers
        # description: The project description
        
        for repo in repositories:
            # The project name and description we pull out of the data
            # provided by Bitbucket.
            project_name = repo['name']
            slug = repo['slug']
            description = repo['description']

            # Get the corresponding project object, if it exists.
            (project, _) = mysite.search.models.Project.objects.get_or_create(
                name=repo['slug'])

            # Get the most recent PortfolioEntry for this person and that
            # project.
            #
            # If there is no such PortfolioEntry, then set its project
            # description to the one provided by Bitbucket.
            portfolio_entry, _ = mysite.profile.models.PortfolioEntry.objects.get_or_create(
                person=person,
                project=project,
                defaults={'project_description':
                          description.rstrip() or project_name})
            # Create a Citation that links to the Bitbucket page
            citation, _ = mysite.profile.models.Citation.objects.get_or_create(
                url = self.url_for_project(bitbucket_username,
                                           slug),
                portfolio_entry = portfolio_entry,
                defaults = dict(
                    contributor_role='Contributed to a repository on Bitbucket.',
                    data_import_attempt = self.get_dia(),
                    languages=''))
            citation.languages = ''
            citation.save_and_check_for_duplicates()

class AbstractOhlohAccountImporter(ProfileImporter):
    SQUASH_THESE_HTTP_CODES = ['404',]

    def ohloh_data_to_citations(self, ohloh_data):
        '''This takes post-processed responses from Ohloh, in a
        particular arbitrary format, and turns them into Citation objects
        suitable to be stored in the OpenHatch database.'''
        for ohloh_contrib_info in ohloh_data:
            self.store_one_ohloh_contrib_info(ohloh_contrib_info)

    def store_one_ohloh_contrib_info(self, ohloh_contrib_info):
        (project, _) = mysite.search.models.Project.objects.get_or_create(
                name=ohloh_contrib_info['project'])
        # FIXME: don't import if blacklisted
        (portfolio_entry, _) = mysite.profile.models.PortfolioEntry.objects.get_or_create(
                person=self.get_dia().person, project=project)
        citation = mysite.profile.models.Citation.create_from_ohloh_contrib_info(ohloh_contrib_info)
        citation.portfolio_entry = portfolio_entry
        citation.data_import_attempt = self.get_dia()
        citation.save_and_check_for_duplicates()

    def url_for_ohloh_query(self, url, params=None, API_KEY=None):
        if API_KEY is None:
            from django.conf import settings
            API_KEY = settings.OHLOH_API_KEY

        my_params = {u'api_key': unicode(API_KEY)}
        if params:
            my_params.update(params)
        params = my_params ; del my_params

        encoded = mysite.base.unicode_sanity.urlencode(params)
        if url[-1] != '?':
            url += u'?'

        url += encoded
        return url

    def parse_ohloh_xml(self, xml_string):
        try:
            s = xml_string
            tree = ET.parse(StringIO.StringIO(s))
        except xml.parsers.expat.ExpatError:
            # well, I'll be. it doesn't parse.
            # There's nothing to do.
            return None

        # Did Ohloh return an error?
        root = tree.getroot()
        if root.find('error') is not None:
            # FIXME: We could log this, but for now, we'll just eat it.
            return None # The callback chain is over.

        return tree

    def xml_tag_to_dict(self, tag):
        '''This method turns the input tag into a dictionary of
        Unicode strings.

        We use this across the Ohloh import code because I feel more
        comfortable passing native Python dictionaries around, rather
        than thick, heavy XML things.

        (That, and dictionaries are easier to use in the test suite.'''
        this = {}
        for child in tag.getchildren():
            if child.text:
                this[unicode(child.tag)] = force_unicode(child.text)
        return this

    def filter_ohloh_xml(self, root, selector, many=False):
        relevant_tag_dicts = []
        interestings = root.findall(selector)
        for interesting in interestings:
            this = self.xml_tag_to_dict(interesting)
            # Good, now we have a dictionary version of the XML tag.
            if many:
                relevant_tag_dicts.append(this)
            else:
                return this

        if many:
            return relevant_tag_dicts

    def enhance_ohloh_contributor_facts(self, c_fs, filter_out_based_on_query=True, override_project_name=None):
        ret = []
        for c_f in c_fs:
            # Ohloh matches on anything containing the username we asked for as a substring,
            # so check that the contributor fact actually matches the whole string (case-insensitive).
            if override_project_name:
                # FIXME: In this case, do a check with Ohloh that we have the right
                # canonicalization of the project name.
                project_data = {'name': override_project_name}
            else:
                if 'analysis_id' not in c_f:
                    continue # this contributor fact is useless
                eyedee = int(c_f['analysis_id'])
                project_data = {'name': eyedee}
                # project_data = self.analysis2projectdata(eyedee)
                # FIXME: BLOCKING

            if filter_out_based_on_query:
                if self.query.lower() != c_f['contributor_name'].lower():
                    continue

            # permalink = generate_contributor_url(
            # project_data['name'],
            # int(c_f['contributor_id'])) # FIXME: BLOCKING
            permalink = 'http://example.com/'
            if 'man_months' in c_f:
                man_months = int(c_f['man_months'])
            else:
                man_months = None # Unknown

            this = dict(
                project=project_data['name'],
                project_homepage_url=project_data.get('homepage_url', None),
                permalink=permalink,
                primary_language=c_f.get('primary_language_nice_name', ''),
                man_months=man_months)
            ret.append(this)
        return ret

    def parse_then_filter_then_interpret_ohloh_xml(self, xml_string, filter_out_based_on_query=True, override_project_name=None):
        tree = self.parse_ohloh_xml(xml_string)
        if tree is None:
            return

        list_of_dicts = self.filter_ohloh_xml(tree, 'result/contributor_fact', many=True)
        if not list_of_dicts:
            return

        list_of_dicts = self.enhance_ohloh_contributor_facts(list_of_dicts, filter_out_based_on_query, override_project_name)

        # Okay, so we know we got some XML back, and that we have converted
        # its tags to unicode<->unicode dictionaries.
        self.ohloh_data_to_citations(list_of_dicts)

class RepositorySearchOhlohImporter(AbstractOhlohAccountImporter):
    BASE_URL = 'http://www.ohloh.net/contributors.xml'

    def getUrlsAndCallbacks(self):
        url = self.url_for_ohloh_query(url=self.BASE_URL,
                                       params={u'query': self.query})

        return [{
                'url': url,
                'errback': self.squashIrrelevantErrors,
                'callback': self.parse_then_filter_then_interpret_ohloh_xml}]

###

class OhlohUsernameImporter(AbstractOhlohAccountImporter):

    def getUrlsAndCallbacksForUsername(self, username):
        # First, we load download the user's profile page and look for
        # (project, contributor_id) pairs.
        #
        # Then, eventually, we will ask the Ohloh API about each of
        # those projects.
        #
        # It would be nice if there were a way to do this using only
        # the Ohloh API, but I don't think there is.

        # FIXME: Handle unicode input for username

        return [{
                'url': ('https://www.ohloh.net/accounts/%s' %
                        urllib.quote(username)),
                'callback': self.process_user_page,
                'errback': self.squashIrrelevantErrors}]

    def getUrlsAndCallbacks(self):
        # First, we load download the user's profile page and look for
        # (project, contributor_id) pairs.
        #
        # Then, eventually, we will ask the Ohloh API about each of
        # those projects.
        #
        # It would be nice if there were a way to do this using only
        # the Ohloh API, but I don't think there is.
        if '@' in self.query:
            # To handle email addresses with Ohloh, all we have to do
            # is turn them into their MD5 hashes.
            #
            # If an account with that username exists, then Ohloh will redirect
            # us to the actual account page.
            #
            # If not, we will probably get a 404.
            hasher = hashlib.md5(); hasher.update(self.query)
            hashed = hasher.hexdigest()
            query = hashed
        else:
            # If it is not an email address, we can just pass it straight through.
            query = self.query

        return self.getUrlsAndCallbacksForUsername(query)

    def getUrlAndCallbackForProjectAndContributor(self, project_name,
                                                  contributor_id):
        base_url = 'https://www.ohloh.net/p/%s/contributors/%d.xml' % (
            urllib.quote(project_name), contributor_id)

        # Since we know that the contributor ID truly does correspond
        # to this OpenHatch user, we pass in filter_out_based_on_query=False.
        #
        # The parse_then_... method typically checks each Ohloh contributor_fact
        # to make sure it is relevant. Since we know they are all relevant,
        # we can skip that check.
        callback = lambda data: self.parse_then_filter_then_interpret_ohloh_xml(data, filter_out_based_on_query=False, override_project_name=project_name)

        return {'url': self.url_for_ohloh_query(base_url),
                'callback': callback,
                'errback': self.squashIrrelevantErrors}

    def process_user_page(self, html_string):
        root = lxml.html.parse(StringIO.StringIO(html_string)).getroot()
        relevant_links = root.cssselect('a.position')
        relevant_hrefs = [link.attrib['href'] for link in relevant_links if '/contributors/' in link.attrib['href']]
        relevant_project_and_contributor_id_pairs = []
        # FIXME: do more logging here someday?
        for href in relevant_hrefs:
            project, contributor_id = re.split('[/][a-z]+[/]', href, 1
                                               )[1].split('/contributors/')
            relevant_project_and_contributor_id_pairs.append(
                (project, int(contributor_id)))

        for (project_name, contributor_id) in relevant_project_and_contributor_id_pairs:
            self.command.call_getPage_on_data_dict(
                self,
                self.getUrlAndCallbackForProjectAndContributor(
                    project_name, contributor_id))

###

SOURCE_TO_CLASS = {
    'db': DebianQA,
    'bb': BitbucketImporter,
    'gh': GithubImporter,
    'lp': LaunchpadProfilePageScraper,
    'rs': RepositorySearchOhlohImporter,
    'oh': OhlohUsernameImporter,
}
