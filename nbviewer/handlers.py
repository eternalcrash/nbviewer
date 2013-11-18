#-----------------------------------------------------------------------------
#  Copyright (C) 2013 The IPython Development Team
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
#-----------------------------------------------------------------------------

import base64
import json
import time
# from concurrent.futures import ThreadPoolExecutor

try:
    # py3
    from http.client import responses
except ImportError:
    from httplib import responses

from tornado import web, gen
from tornado.escape import utf8
from tornado.log import app_log, access_log

from .render import render_notebook, NbFormatError

#-----------------------------------------------------------------------------
# Handler classes
#-----------------------------------------------------------------------------

class BaseHandler(web.RequestHandler):
    """Base Handler class with common utilities"""
    
    @property
    def exporter(self):
        return self.settings['exporter']
    
    @property
    def github_client(self):
        return self.settings['github_client']
    
    @property
    def client(self):
        return self.settings['client']
    
    @property
    def cache(self):
        return self.settings['cache']
    
    @property
    def cache_expiry(self):
        return self.settings.get('cache_expiry', 60)
    
    @property
    def pool(self):
        return self.settings['pool']
    
    #---------------------------------------------------------------
    # template rendering
    #---------------------------------------------------------------
    
    def get_template(self, name):
        """Return the jinja template object for a given name"""
        return self.settings['jinja2_env'].get_template(name)
    
    def render_template(self, name, **ns):
        ns.update(self.template_namespace)
        template = self.get_template(name)
        return template.render(**ns)
    
    @property
    def template_namespace(self):
        return {}
    
    #---------------------------------------------------------------
    # response caching
    #---------------------------------------------------------------
    
    @gen.coroutine
    def cache_and_finish(self, content=''):
        self.write(content)
        
        burl = utf8(self.request.uri)
        bcontent = utf8(content)
        
        yield self.cache.set(
            burl, bcontent, int(time.time() + self.cache_expiry),
        )


class CustomErrorHandler(web.ErrorHandler, BaseHandler):
    """Render errors with custom template"""
    def get_error_html(self, status_code, **kwargs):
        try:
            html = self.render_template('%d.html' % status_code)
        except Exception as e:
            app_log.error("no template", exc_info=True)
            html = self.render_template('error.html',
                status_code=status_code,
                status_message=responses[status_code]
            )
        return html


class IndexHandler(BaseHandler):
    def get(self):
        self.finish(self.render_template('index.html'))


class FAQHandler(BaseHandler):
    def get(self):
        self.finish(self.render_template('faq.md'))


def cached(method):
    @gen.coroutine
    def cached_method(self, *args, **kwargs):
        cached_response = yield self.cache.get(self.request.uri)
        if cached_response is not None:
            app_log.debug("cache hit %s", self.request.uri)
            self.write(cached_response)
        else:
            app_log.debug("cache miss %s", self.request.uri)
            # call the wrapped method
            yield method(self, *args, **kwargs)
    
    return cached_method


class RenderingHandler(BaseHandler):
    @gen.coroutine
    def finish_notebook(self, nbjson, url, msg=None):
        if msg is None:
            msg = url
        try:
            nbhtml, config = yield self.pool.submit(
                render_notebook, self.exporter, nbjson, url=url
            )
        except NbFormatError as e:
            app_log.error("Failed to render %s", msg, exc_info=True)
            raise web.HTTPError(400)
        
        html = self.render_template('notebook.html', body=nbhtml, **config)
        yield self.cache_and_finish(html)

class URLHandler(RenderingHandler):
    @cached
    @gen.coroutine
    def get(self, secure, url):
        proto = 'http' + secure
        
        remote_url = "{}://{}".format(proto, url)
        response = yield self.client.fetch(remote_url)
        if response.error:
            response.rethrow()
        
        nbjson = response.body.decode('utf8')
        yield self.finish_notebook(nbjson, remote_url, "file from url: %s" % remote_url)

class GistHandler(RenderingHandler):
    @cached
    @gen.coroutine
    def get(self, gist_id, filename=''):
        response = yield self.github_client.get_gist(gist_id)
        if response.error:
            response.rethrow()
        
        data = json.loads(response.body.decode('utf8'))
        gist_id=data['id']
        files = data['files']
        if len(files) == 1:
            filename = list(files.keys())[0]
        if filename:
            file = files[filename]
            nbjson = file['content']
            yield self.finish_notebook(nbjson, file['raw_url'], "gist: %s" % gist_id)
        elif filename:
            raise web.HTTPError(404, "No such file in gist: %s (%s)", filename, list(files.keys()))
        else:
            entries = []
            for filename, file in files.items():
                entries.append(dict(
                    path=filename,
                    url='/%s/%s' % (gist_id, filename),
                ))
            html = self.render_template('gistlist.html', entries=entries)
            yield self.cache_and_finish(html)

class GistRedirectHandler(BaseHandler):
    def get(self, gist_id, file=''):
        new_url = '/gist/%s' % gist_id
        if file:
            new_url = "%s/%s" % (new_url, file)
        
        app_log.info("Redirecting %s to %s", self.request.uri, new_url)
        self.redirect(new_url)

class RawGitHubURLHandler(BaseHandler):
    def get(self, user, repo, path):
        new_url = '/github/{user}/{repo}/blob/{path}'.format(
            user=user, repo=repo, path=path,
        )
        app_log.info("Redirecting %s to %s", self.request.uri, new_url)
        self.redirect(new_url)

class GitHubRedirectHandler(BaseHandler):
    def get(self, user, repo, ref, path):
        new_url = '/github/{user}/{repo}/{ref}/{path}'.format(**locals())
        app_log.info("Redirecting %s to %s", self.request.uri, new_url)
        self.redirect(new_url)

class GitHubUserHandler(BaseHandler):
    @cached
    @gen.coroutine
    def get(self, user):
        response = yield self.github_client.get_repos(user)
        if response.error:
            response.rethrow()
        repos = json.loads(response.body.decode('utf8'))
        entries = []
        for repo in repos:
            entries.append(dict(
                url=repo['name'],
                name=repo['name'],
            ))
        html = self.render_template("userview.html", entries=entries)
        yield self.cache_and_finish(html)

class GitHubRepoHandler(BaseHandler):
    def get(self, user, repo):
        self.redirect("/github/%s/%s/tree/master/" % (user, repo))


class GitHubTreeHandler(BaseHandler):
    @cached
    @gen.coroutine
    def get(self, user, repo, ref, path):
        if not self.request.uri.endswith('/'):
            self.redirect(self.request.uri + '/')
            return
        path = path.rstrip('/')
        response = yield self.github_client.get_contents(user, repo, path, ref=ref)
        if response.error:
            response.rethrow()
        contents = json.loads(response.body.decode('utf8'))
        if not isinstance(contents, list):
            app_log.info("{user}/{repo}/{ref}/{path} not tree, redirecting to blob",
                extra=dict(user=user, repo=repo, ref=ref, path=path)
            )
            self.redirect(
                "/github/{user}/{repo}/blob/{ref}/{path}".format(
                    user=user, repo=repo, ref=ref, path=path,
                )
            )
            return
        
        base_url = "/github/{user}/{repo}/tree/{ref}".format(
            user=user, repo=repo, ref=ref,
        )
        path_list = [{
            'url' : base_url,
            'name' : repo,
        }]
        if path:
            for name in path.split('/'):
                href = base_url = "%s/%s" % (base_url, name)
                path_list.append({
                    'url' : base_url,
                    'name' : name,
                })
        
        entries = []
        for file in contents:
            e = {}
            e['name'] = file['name']
            e['url'] = '/github/{user}/{repo}/{app}/{ref}/{path}'.format(
                user=user, repo=repo, ref=ref, path=file['path'],
                app='tree' if file['type'] == 'dir' else 'blob'
            )
            e['class'] = 'icon-folder-open' if file['type'] == 'dir' else 'icon-file'
            entries.append(e)
        # print path, path_list
        html = self.render_template("treelist.html", entries=entries, path_list=path_list)
        yield self.cache_and_finish(html)
    

class GitHubBlobHandler(RenderingHandler):
    @cached
    @gen.coroutine
    def get(self, user, repo, ref, path):
        response = yield self.github_client.get_contents(user, repo, path, ref=ref)
        if response.error:
            response.rethrow()
        
        contents = json.loads(response.body.decode('utf8'))
        if isinstance(contents, list):
            app_log.info("{user}/{repo}/{ref}/{path} not blob, redirecting to tree",
                extra=dict(user=user, repo=repo, ref=ref, path=path)
            )
            self.redirect(
                "/github/{user}/{repo}/tree/{ref}/{path}/".format(
                    user=user, repo=repo, ref=ref, path=path
                )
            )
            return
        
        try:
            filedata = base64.decodestring(contents['content'].encode('ascii'))
        except Exception as e:
            app_log.error("Failed to load file from GitHub: %s", contents['url'], exc_info=True)
            raise web.HTTPError(400)
        
        if contents['name'].endswith('.ipynb'):
            try:
                nbjson = filedata.decode('utf8')
            except Exception as e:
                app_log.error("Failed to decode notebook: %s", contents['url'], exc_info=True)
                raise web.HTTPError(400)
            raw_url = "https://raw.github.com/{user}/{repo}/{ref}/{path}".format(
                user=user, repo=repo, ref=ref, path=path
            )
            yield self.finish_notebook(nbjson, raw_url, "file from GitHub: %s" % contents['url'])
        else:
            self.set_header("Content-Type", "text/plain")
            self.write(filedata)


class FilesRedirectHandler(BaseHandler):
    def get(self, before_files, after_files):
        app_log.info("Redirecting %s to %s", before_files, after_files)
        self.redirect("%s/%s" % (before_files, after_files))


class AddSlashHandler(BaseHandler):
    def get(self, *args, **kwargs):
        self.redirect(self.request.uri + '/')

class RemoveSlashHandler(BaseHandler):
    def get(self, *args, **kwargs):
        self.redirect(self.request.uri.rstrip('/'))


#-----------------------------------------------------------------------------
# Default handler URL mapping
#-----------------------------------------------------------------------------

handlers = [
    ('/', IndexHandler),
    ('/index.html', IndexHandler),
    ('/faq', FAQHandler),
    (r'/url[s]?/github\.com/([^\/]+)/([^\/]+)/(?:tree|blob)/([^\/]+)/(.*)', GitHubRedirectHandler),
    (r'/url[s]?/raw\.?github\.com/([^\/]+)/([^\/]+)/(.*)', RawGitHubURLHandler),
    (r'/url([s]?)/(.*)', URLHandler),

    (r'/github/([\w\-]+)', AddSlashHandler),
    (r'/github/([\w\-]+)/', GitHubUserHandler),
    (r'/github/([\w\-]+)/([\w\-]+)', AddSlashHandler),
    (r'/github/([\w\-]+)/([\w\-]+)/', GitHubRepoHandler),
    (r'/github/([\w\-]+)/([^\/]+)/blob/([^\/]+)/(.*)/', RemoveSlashHandler),
    (r'/github/([\w\-]+)/([^\/]+)/blob/([^\/]+)/(.*)', GitHubBlobHandler),
    (r'/github/([\w\-]+)/([^\/]+)/tree/([^\/]+)', AddSlashHandler),
    (r'/github/([\w\-]+)/([^\/]+)/tree/([^\/]+)/(.*)', GitHubTreeHandler),

    (r'/gist/([a-fA-F0-9]+)', GistHandler),
    (r'/gist/([a-fA-F0-9]+)/(.*)', GistHandler),
    (r'/([a-fA-F0-9]+)', GistRedirectHandler),
    (r'/([a-fA-F0-9]+)/(.*)', GistRedirectHandler),
]