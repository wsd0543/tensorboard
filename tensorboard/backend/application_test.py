# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Unit tests for application package."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import functools
import json
import ntpath
import os
import posixpath
import shutil
import socket
import tempfile
import time

import six

try:
    # python version >= 3.3
    from unittest import mock
except ImportError:
    import mock  # pylint: disable=unused-import

from werkzeug import test as werkzeug_test
from werkzeug import wrappers

from tensorboard import errors
from tensorboard import plugin_util
from tensorboard import test as tb_test
from tensorboard.backend import application
from tensorboard.backend.event_processing import (
    plugin_event_multiplexer as event_multiplexer,
)
from tensorboard.data import provider
from tensorboard.plugins import base_plugin


class FakeFlags(object):
    def __init__(
        self,
        logdir,
        logdir_spec="",
        purge_orphaned_data=True,
        reload_interval=60,
        samples_per_plugin=None,
        max_reload_threads=1,
        reload_task="auto",
        window_title="",
        path_prefix="",
        reload_multifile=False,
        reload_multifile_inactive_secs=4000,
        generic_data="auto",
    ):
        self.logdir = logdir
        self.logdir_spec = logdir_spec
        self.purge_orphaned_data = purge_orphaned_data
        self.reload_interval = reload_interval
        self.samples_per_plugin = samples_per_plugin or {}
        self.max_reload_threads = max_reload_threads
        self.reload_task = reload_task
        self.window_title = window_title
        self.path_prefix = path_prefix
        self.reload_multifile = reload_multifile
        self.reload_multifile_inactive_secs = reload_multifile_inactive_secs
        self.generic_data = generic_data


class FakePlugin(base_plugin.TBPlugin):
    """A plugin with no functionality."""

    def __init__(
        self,
        context=None,
        plugin_name="foo",
        is_active_value=True,
        routes_mapping={},
        element_name_value=None,
        es_module_path_value=None,
        is_ng_component=False,
        construction_callback=None,
        data_plugin_names=None,
    ):
        """Constructs a fake plugin.

        Args:
          context: The TBContext magic container. Contains properties that are
            potentially useful to this plugin.
          plugin_name: The name of this plugin.
          is_active_value: Whether the plugin is active.
          routes_mapping: A dictionary mapping from route (string URL path) to the
            method called when a user issues a request to that route.
          es_module_path_value: An optional string value that indicates a frontend
            module entry to the plugin. Must be one of the keys of routes_mapping.
          is_ng_component: Whether this plugin is of the built-in Angular-based
            type.
          construction_callback: An optional callback called when the plugin is
            constructed. The callback is passed the TBContext.
        """
        self.plugin_name = plugin_name
        self._is_active_value = is_active_value
        self._routes_mapping = routes_mapping
        self._element_name_value = element_name_value
        self._es_module_path_value = es_module_path_value
        self._is_ng_component = is_ng_component

        if data_plugin_names is not None:
            self.data_plugin_names = lambda: data_plugin_names

        if construction_callback:
            construction_callback(context)

    def get_plugin_apps(self):
        """Returns a mapping from routes to handlers offered by this plugin.

        Returns:
          A dictionary mapping from routes to handlers offered by this plugin.
        """
        return self._routes_mapping

    def is_active(self):
        """Returns whether this plugin is active.

        Returns:
          A boolean. Whether this plugin is active.
        """
        return self._is_active_value

    def frontend_metadata(self):
        return base_plugin.FrontendMetadata(
            element_name=self._element_name_value,
            es_module_path=self._es_module_path_value,
            is_ng_component=self._is_ng_component,
        )


class FakePluginLoader(base_plugin.TBLoader):
    """Pass-through loader for FakePlugin with arbitrary arguments."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def load(self, context):
        return FakePlugin(context, **self._kwargs)


class FakeDataProvider(provider.DataProvider):
    """No-op `DataProvider`; override methods on specific instances."""

    def __init__(self):
        pass

    def list_runs(self, experiment_id):
        raise NotImplementedError()

    def list_scalars(self, experiment_id):
        raise NotImplementedError()

    def read_scalars(self, experiment_id):
        raise NotImplementedError()


class HandlingErrorsTest(tb_test.TestCase):
    def test_successful_response_passes_through(self):
        @application._handling_errors
        @wrappers.Request.application
        def app(request):
            return wrappers.Response(
                "All is well", 200, content_type="text/html"
            )

        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        response = server.get("/")
        self.assertEqual(response.get_data(), b"All is well")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type"), "text/html")

    def test_public_errors_serve_response(self):
        @application._handling_errors
        @wrappers.Request.application
        def app(request):
            raise errors.NotFoundError("no scalar data for run=foo, tag=bar")

        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        response = server.get("/")
        self.assertEqual(
            response.get_data(),
            b"Not found: no scalar data for run=foo, tag=bar",
        )
        self.assertEqual(response.status_code, 404)
        self.assertStartsWith(
            response.headers.get("Content-Type"), "text/plain"
        )

    def test_internal_errors_propagate(self):
        @application._handling_errors
        @wrappers.Request.application
        def app(request):
            raise ValueError("something borked internally")

        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        with self.assertRaises(ValueError) as cm:
            response = server.get("/")
        self.assertEqual(str(cm.exception), "something borked internally")

    def test_passes_through_non_wsgi_args(self):
        class C(object):
            @application._handling_errors
            def __call__(self, environ, start_response):
                start_response("200 OK", [("Content-Type", "text/html")])
                yield b"All is well"

        app = C()
        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        response = server.get("/")
        self.assertEqual(response.get_data(), b"All is well")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type"), "text/html")


class ApplicationTest(tb_test.TestCase):
    def setUp(self):
        plugins = [
            FakePlugin(plugin_name="foo"),
            FakePlugin(
                plugin_name="bar",
                is_active_value=False,
                element_name_value="tf-bar-dashboard",
            ),
            FakePlugin(
                plugin_name="baz",
                routes_mapping={"/esmodule": lambda req: None,},
                es_module_path_value="/esmodule",
            ),
            FakePlugin(
                plugin_name="qux", is_active_value=False, is_ng_component=True,
            ),
        ]
        app = application.TensorBoardWSGI(plugins)
        self._install_server(app)

    def _install_server(self, app):
        self.server = werkzeug_test.Client(app, wrappers.BaseResponse)

    def _get_json(self, path):
        response = self.server.get(path)
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("Content-Type")
        )
        return json.loads(response.get_data().decode("utf-8"))

    def testBasicStartup(self):
        """Start the server up and then shut it down immediately."""
        pass

    def testRequestNonexistentPage(self):
        """Request a page that doesn't exist; it should 404."""
        response = self.server.get("/asdf")
        self.assertEqual(404, response.status_code)

    def testPluginsListing(self):
        """Test the format of the data/plugins_listing endpoint."""
        parsed_object = self._get_json("/data/plugins_listing")
        self.assertEqual(
            parsed_object,
            {
                "foo": {
                    "enabled": True,
                    "loading_mechanism": {"type": "NONE"},
                    "remove_dom": False,
                    "tab_name": "foo",
                    "disable_reload": False,
                },
                "bar": {
                    "enabled": False,
                    "loading_mechanism": {
                        "type": "CUSTOM_ELEMENT",
                        "element_name": "tf-bar-dashboard",
                    },
                    "tab_name": "bar",
                    "remove_dom": False,
                    "disable_reload": False,
                },
                "baz": {
                    "enabled": True,
                    "loading_mechanism": {
                        "type": "IFRAME",
                        "module_path": "/data/plugin/baz/esmodule",
                    },
                    "tab_name": "baz",
                    "remove_dom": False,
                    "disable_reload": False,
                },
                "qux": {
                    "enabled": False,
                    "loading_mechanism": {"type": "NG_COMPONENT",},
                    "tab_name": "qux",
                    "remove_dom": False,
                    "disable_reload": False,
                },
            },
        )

    def testPluginsListingWithDataProviderListActivePlugins(self):
        prov = FakeDataProvider()
        self.assertIsNotNone(prov.list_plugins)
        prov.list_plugins = lambda experiment_id: ("foo", "bar")

        plugins = [
            FakePlugin(plugin_name="foo", is_active_value=False),
            FakePlugin(
                plugin_name="bar", is_active_value=False, data_plugin_names=(),
            ),
            FakePlugin(plugin_name="baz", is_active_value=False),
            FakePlugin(
                plugin_name="quux",
                is_active_value=False,
                data_plugin_names=("bar", "baz"),
            ),
            FakePlugin(
                plugin_name="zod",
                is_active_value=True,
                data_plugin_names=("none_but_should_fall_back"),
            ),
        ]
        app = application.TensorBoardWSGI(plugins, data_provider=prov)
        self._install_server(app)

        parsed_object = self._get_json("/data/plugins_listing")
        actives = {k: v["enabled"] for (k, v) in parsed_object.items()}
        self.assertEqual(
            actives,
            {
                "foo": True,  # directly has data
                "bar": False,  # has data, but does not depend on itself
                "baz": False,  # no data, and no dependencies
                "quux": True,  # no data, but depends on "bar"
                "zod": True,  # no data, but `is_active` return `True`
            },
        )

    def testPluginsListingRobustToIsActiveFailures(self):
        real_is_active = FakePlugin.is_active

        def fake_is_active(self):
            if self.plugin_name == "foo":
                raise RuntimeError("this plugin is actually radioactive")
            else:
                return real_is_active(self)

        with mock.patch.object(FakePlugin, "is_active", fake_is_active):
            parsed_object = self._get_json("/data/plugins_listing")
        self.assertEqual(parsed_object["foo"]["enabled"], False)
        self.assertEqual(parsed_object["baz"]["enabled"], True)

    def testPluginsListingWithExperimentalPlugin(self):
        plugins = [
            FakePlugin(plugin_name="bar"),
            FakePlugin(plugin_name="foo"),
            FakePlugin(plugin_name="bazz"),
        ]
        app = application.TensorBoardWSGI(plugins, experimental_plugins=["foo"])
        self._install_server(app)

        plugins_without_flag = self._get_json("/data/plugins_listing")
        self.assertIsNotNone(plugins_without_flag.get("bar"))
        self.assertIsNone(plugins_without_flag.get("foo"))
        self.assertIsNotNone(plugins_without_flag.get("bazz"))

        plugins_with_flag = self._get_json(
            "/data/plugins_listing?experimentalPlugin=foo"
        )
        self.assertIsNotNone(plugins_with_flag.get("bar"))
        self.assertIsNotNone(plugins_with_flag.get("foo"))
        self.assertIsNotNone(plugins_with_flag.get("bazz"))

        plugins_with_useless_flag = self._get_json(
            "/data/plugins_listing?experimentalPlugin=bar"
        )
        self.assertIsNotNone(plugins_with_useless_flag.get("bar"))
        self.assertIsNone(plugins_with_useless_flag.get("foo"))
        self.assertIsNotNone(plugins_with_useless_flag.get("bazz"))

    def testPluginsListingWithMultipleExperimentalPlugins(self):
        plugins = [
            FakePlugin(plugin_name="bar"),
            FakePlugin(plugin_name="foo"),
            FakePlugin(plugin_name="bazz"),
        ]
        app = application.TensorBoardWSGI(
            plugins, experimental_plugins=["bar", "bazz"]
        )
        self._install_server(app)

        plugins_without_flag = self._get_json("/data/plugins_listing")
        self.assertIsNone(plugins_without_flag.get("bar"))
        self.assertIsNotNone(plugins_without_flag.get("foo"))
        self.assertIsNone(plugins_without_flag.get("bazz"))

        plugins_with_one_flag = self._get_json(
            "/data/plugins_listing?experimentalPlugin=bar"
        )
        self.assertIsNotNone(plugins_with_one_flag.get("bar"))
        self.assertIsNotNone(plugins_with_one_flag.get("foo"))
        self.assertIsNone(plugins_with_one_flag.get("bazz"))

        plugins_with_multiple_flags = self._get_json(
            "/data/plugins_listing?experimentalPlugin=bar&experimentalPlugin=bazz"
        )
        self.assertIsNotNone(plugins_with_multiple_flags.get("bar"))
        self.assertIsNotNone(plugins_with_multiple_flags.get("foo"))
        self.assertIsNotNone(plugins_with_multiple_flags.get("bazz"))

    def testPluginEntry(self):
        """Test the data/plugin_entry.html endpoint."""
        response = self.server.get("/data/plugin_entry.html?name=baz")
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "text/html; charset=utf-8", response.headers.get("Content-Type")
        )

        document = response.get_data().decode("utf-8")
        self.assertIn('<head><base href="plugin/baz/" /></head>', document)
        self.assertIn(
            'import("./esmodule").then((m) => void m.render());', document
        )
        # base64 sha256 of above script
        self.assertIn(
            "'sha256-3KGOnqHhLsX2RmjH/K2DurN9N2qtApZk5zHdSPg4LcA='",
            response.headers.get("Content-Security-Policy"),
        )

        for name in ["bazz", "baz "]:
            response = self.server.get("/data/plugin_entry.html?name=%s" % name)
            self.assertEqual(404, response.status_code)

        for name in ["foo", "bar"]:
            response = self.server.get("/data/plugin_entry.html?name=%s" % name)
            self.assertEqual(400, response.status_code)
            self.assertEqual(
                response.get_data().decode("utf-8"),
                "Plugin is not module loadable",
            )

    def testPluginEntryBadModulePath(self):
        plugins = [
            FakePlugin(
                plugin_name="mallory", es_module_path_value="//pwn.tb/somepath"
            ),
        ]
        app = application.TensorBoardWSGI(plugins)
        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        with six.assertRaisesRegex(
            self, ValueError, "Expected es_module_path to be non-absolute path"
        ):
            server.get("/data/plugin_entry.html?name=mallory")

    def testNgComponentPluginWithIncompatibleSetElementName(self):
        plugins = [
            FakePlugin(
                plugin_name="quux",
                is_ng_component=True,
                element_name_value="incompatible",
            ),
        ]
        app = application.TensorBoardWSGI(plugins)
        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        with six.assertRaisesRegex(
            self, ValueError, "quux.*declared.*both Angular.*legacy"
        ):
            server.get("/data/plugins_listing")

    def testNgComponentPluginWithIncompatiblEsModulePath(self):
        plugins = [
            FakePlugin(
                plugin_name="quux",
                is_ng_component=True,
                es_module_path_value="//incompatible",
            ),
        ]
        app = application.TensorBoardWSGI(plugins)
        server = werkzeug_test.Client(app, wrappers.BaseResponse)
        with six.assertRaisesRegex(
            self, ValueError, "quux.*declared.*both Angular.*iframed"
        ):
            server.get("/data/plugins_listing")


class ApplicationBaseUrlTest(tb_test.TestCase):
    path_prefix = "/test"

    def setUp(self):
        plugins = [
            FakePlugin(plugin_name="foo"),
            FakePlugin(
                plugin_name="bar",
                is_active_value=False,
                element_name_value="tf-bar-dashboard",
            ),
            FakePlugin(
                plugin_name="baz",
                routes_mapping={"/esmodule": lambda req: None,},
                es_module_path_value="/esmodule",
            ),
        ]
        app = application.TensorBoardWSGI(plugins, path_prefix=self.path_prefix)
        self.server = werkzeug_test.Client(app, wrappers.BaseResponse)

    def _get_json(self, path):
        response = self.server.get(path)
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("Content-Type")
        )
        return json.loads(response.get_data().decode("utf-8"))

    def testBaseUrlRequest(self):
        """Base URL should redirect to "/" for proper relative URLs."""
        response = self.server.get(self.path_prefix)
        self.assertEqual(301, response.status_code)

    def testBaseUrlRequestNonexistentPage(self):
        """Request a page that doesn't exist; it should 404."""
        response = self.server.get(self.path_prefix + "/asdf")
        self.assertEqual(404, response.status_code)

    def testBaseUrlNonexistentPluginsListing(self):
        """Test the format of the data/plugins_listing endpoint."""
        response = self.server.get("/non_existent_prefix/data/plugins_listing")
        self.assertEqual(404, response.status_code)

    def testPluginsListing(self):
        """Test the format of the data/plugins_listing endpoint."""
        parsed_object = self._get_json(
            self.path_prefix + "/data/plugins_listing"
        )
        self.assertEqual(
            parsed_object,
            {
                "foo": {
                    "enabled": True,
                    "loading_mechanism": {"type": "NONE"},
                    "remove_dom": False,
                    "tab_name": "foo",
                    "disable_reload": False,
                },
                "bar": {
                    "enabled": False,
                    "loading_mechanism": {
                        "type": "CUSTOM_ELEMENT",
                        "element_name": "tf-bar-dashboard",
                    },
                    "tab_name": "bar",
                    "remove_dom": False,
                    "disable_reload": False,
                },
                "baz": {
                    "enabled": True,
                    "loading_mechanism": {
                        "type": "IFRAME",
                        "module_path": "/test/data/plugin/baz/esmodule",
                    },
                    "tab_name": "baz",
                    "remove_dom": False,
                    "disable_reload": False,
                },
            },
        )


class ApplicationPluginNameTest(tb_test.TestCase):
    def testSimpleName(self):
        application.TensorBoardWSGI(plugins=[FakePlugin(plugin_name="scalars")])

    def testComprehensiveName(self):
        application.TensorBoardWSGI(
            plugins=[FakePlugin(plugin_name="Scalar-Dashboard_3000")]
        )

    def testNameIsNone(self):
        with six.assertRaisesRegex(self, ValueError, r"no plugin_name"):
            application.TensorBoardWSGI(plugins=[FakePlugin(plugin_name=None)])

    def testEmptyName(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid name"):
            application.TensorBoardWSGI(plugins=[FakePlugin(plugin_name="")])

    def testNameWithSlashes(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid name"):
            application.TensorBoardWSGI(
                plugins=[FakePlugin(plugin_name="scalars/data")]
            )

    def testNameWithPeriods(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid name"):
            application.TensorBoardWSGI(
                plugins=[FakePlugin(plugin_name="scalars.data")]
            )

    def testNameWithSpaces(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid name"):
            application.TensorBoardWSGI(
                plugins=[FakePlugin(plugin_name="my favorite plugin")]
            )

    def testDuplicateName(self):
        with six.assertRaisesRegex(self, ValueError, r"Duplicate"):
            application.TensorBoardWSGI(
                plugins=[
                    FakePlugin(plugin_name="scalars"),
                    FakePlugin(plugin_name="scalars"),
                ]
            )


class ApplicationPluginRouteTest(tb_test.TestCase):
    def _make_plugin(self, route):
        return FakePlugin(
            plugin_name="foo",
            routes_mapping={route: lambda environ, start_response: None},
        )

    def testNormalRoute(self):
        application.TensorBoardWSGI([self._make_plugin("/runs")])

    def testWildcardRoute(self):
        application.TensorBoardWSGI([self._make_plugin("/foo/*")])

    def testNonPathComponentWildcardRoute(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid route"):
            application.TensorBoardWSGI([self._make_plugin("/foo*")])

    def testMultiWildcardRoute(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid route"):
            application.TensorBoardWSGI([self._make_plugin("/foo/*/bar/*")])

    def testInternalWildcardRoute(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid route"):
            application.TensorBoardWSGI([self._make_plugin("/foo/*/bar")])

    def testEmptyRoute(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid route"):
            application.TensorBoardWSGI([self._make_plugin("")])

    def testSlashlessRoute(self):
        with six.assertRaisesRegex(self, ValueError, r"invalid route"):
            application.TensorBoardWSGI([self._make_plugin("runaway")])


class MakePluginLoaderTest(tb_test.TestCase):
    def testMakePluginLoader_pluginClass(self):
        loader = application.make_plugin_loader(FakePlugin)
        self.assertIsInstance(loader, base_plugin.BasicLoader)
        self.assertIs(loader.plugin_class, FakePlugin)

    def testMakePluginLoader_pluginLoaderClass(self):
        loader = application.make_plugin_loader(FakePluginLoader)
        self.assertIsInstance(loader, FakePluginLoader)

    def testMakePluginLoader_pluginLoader(self):
        loader = FakePluginLoader()
        self.assertIs(loader, application.make_plugin_loader(loader))

    def testMakePluginLoader_invalidType(self):
        with six.assertRaisesRegex(self, TypeError, "FakePlugin"):
            application.make_plugin_loader(FakePlugin())


class GetEventFileActiveFilterTest(tb_test.TestCase):
    def testDisabled(self):
        flags = FakeFlags("logdir", reload_multifile=False)
        self.assertIsNone(application._get_event_file_active_filter(flags))

    def testInactiveSecsZero(self):
        flags = FakeFlags(
            "logdir", reload_multifile=True, reload_multifile_inactive_secs=0
        )
        self.assertIsNone(application._get_event_file_active_filter(flags))

    def testInactiveSecsNegative(self):
        flags = FakeFlags(
            "logdir", reload_multifile=True, reload_multifile_inactive_secs=-1
        )
        filter_fn = application._get_event_file_active_filter(flags)
        self.assertTrue(filter_fn(0))
        self.assertTrue(filter_fn(time.time()))
        self.assertTrue(filter_fn(float("inf")))

    def testInactiveSecs(self):
        flags = FakeFlags(
            "logdir", reload_multifile=True, reload_multifile_inactive_secs=10
        )
        filter_fn = application._get_event_file_active_filter(flags)
        with mock.patch.object(time, "time") as mock_time:
            mock_time.return_value = 100
            self.assertFalse(filter_fn(0))
            self.assertFalse(filter_fn(time.time() - 11))
            self.assertTrue(filter_fn(time.time() - 10))
            self.assertTrue(filter_fn(time.time()))
            self.assertTrue(filter_fn(float("inf")))


class ParseEventFilesSpecTest(tb_test.TestCase):
    def assertPlatformSpecificLogdirParsing(self, pathObj, logdir, expected):
        """A custom assertion to test :func:`parse_event_files_spec` under
        various systems.

        Args:
            pathObj: a custom replacement object for `os.path`, typically
              `posixpath` or `ntpath`
            logdir: the string to be parsed by
              :func:`~application.parse_event_files_spec`
            expected: the expected dictionary as returned by
              :func:`~application.parse_event_files_spec`
        """

        with mock.patch("os.path", pathObj):
            self.assertEqual(
                application.parse_event_files_spec(logdir), expected
            )

    def testBasic(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "/lol/cat", {"/lol/cat": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, r"C:\lol\cat", {r"C:\lol\cat": None}
        )

    def testRunName(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "lol:/cat", {"/cat": "lol"}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "lol:C:\\cat", {"C:\\cat": "lol"}
        )

    def testPathWithColonThatComesAfterASlash_isNotConsideredARunName(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "/lol:/cat", {"/lol:/cat": None}
        )

    def testExpandsUser(self):
        oldhome = os.environ.get("HOME", None)
        try:
            os.environ["HOME"] = "/usr/eliza"
            self.assertPlatformSpecificLogdirParsing(
                posixpath, "~/lol/cat~dog", {"/usr/eliza/lol/cat~dog": None}
            )
            os.environ["HOME"] = r"C:\Users\eliza"
            self.assertPlatformSpecificLogdirParsing(
                ntpath, r"~\lol\cat~dog", {r"C:\Users\eliza\lol\cat~dog": None}
            )
        finally:
            if oldhome is not None:
                os.environ["HOME"] = oldhome

    def testExpandsUserForMultipleDirectories(self):
        oldhome = os.environ.get("HOME", None)
        try:
            os.environ["HOME"] = "/usr/eliza"
            self.assertPlatformSpecificLogdirParsing(
                posixpath,
                "a:~/lol,b:~/cat",
                {"/usr/eliza/lol": "a", "/usr/eliza/cat": "b"},
            )
            os.environ["HOME"] = r"C:\Users\eliza"
            self.assertPlatformSpecificLogdirParsing(
                ntpath,
                r"aa:~\lol,bb:~\cat",
                {r"C:\Users\eliza\lol": "aa", r"C:\Users\eliza\cat": "bb"},
            )
        finally:
            if oldhome is not None:
                os.environ["HOME"] = oldhome

    def testMultipleDirectories(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "/a,/b", {"/a": None, "/b": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "C:\\a,C:\\b", {"C:\\a": None, "C:\\b": None}
        )

    def testNormalizesPaths(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "/lol/.//cat/../cat", {"/lol/cat": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "C:\\lol\\.\\\\cat\\..\\cat", {"C:\\lol\\cat": None}
        )

    def testAbsolutifies(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "lol/cat", {posixpath.realpath("lol/cat"): None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "lol\\cat", {ntpath.realpath("lol\\cat"): None}
        )

    def testRespectsGCSPath(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "gs://foo/path", {"gs://foo/path": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "gs://foo/path", {"gs://foo/path": None}
        )

    def testRespectsHDFSPath(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "hdfs://foo/path", {"hdfs://foo/path": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "hdfs://foo/path", {"hdfs://foo/path": None}
        )

    def testDoesNotExpandUserInGCSPath(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "gs://~/foo/path", {"gs://~/foo/path": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "gs://~/foo/path", {"gs://~/foo/path": None}
        )

    def testDoesNotNormalizeGCSPath(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "gs://foo/./path//..", {"gs://foo/./path//..": None}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "gs://foo/./path//..", {"gs://foo/./path//..": None}
        )

    def testRunNameWithGCSPath(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "lol:gs://foo/path", {"gs://foo/path": "lol"}
        )
        self.assertPlatformSpecificLogdirParsing(
            ntpath, "lol:gs://foo/path", {"gs://foo/path": "lol"}
        )

    def testSingleLetterGroup(self):
        self.assertPlatformSpecificLogdirParsing(
            posixpath, "A:/foo/path", {"/foo/path": "A"}
        )
        # single letter groups are not supported on Windows
        with self.assertRaises(AssertionError):
            self.assertPlatformSpecificLogdirParsing(
                ntpath, "A:C:\\foo\\path", {"C:\\foo\\path": "A"}
            )


class TensorBoardPluginsTest(tb_test.TestCase):
    def setUp(self):
        self.context = None
        dummy_assets_zip_provider = lambda: None
        # The application should have added routes for both plugins.
        self.app = application.standard_tensorboard_wsgi(
            FakeFlags(logdir=self.get_temp_dir()),
            [
                FakePluginLoader(
                    plugin_name="foo",
                    is_active_value=True,
                    routes_mapping={"/foo_route": self._foo_handler},
                    construction_callback=self._construction_callback,
                ),
                FakePluginLoader(
                    plugin_name="bar",
                    is_active_value=True,
                    routes_mapping={
                        "/bar_route": self._bar_handler,
                        "/wildcard/*": self._wildcard_handler,
                        "/wildcard/special/*": self._wildcard_special_handler,
                        "/wildcard/special/exact": self._foo_handler,
                    },
                    construction_callback=self._construction_callback,
                ),
                FakePluginLoader(
                    plugin_name="whoami",
                    routes_mapping={"/eid": self._eid_handler,},
                ),
            ],
            dummy_assets_zip_provider,
        )

        self.server = werkzeug_test.Client(self.app, wrappers.BaseResponse)

    def _construction_callback(self, context):
        """Called when a plugin is constructed."""
        self.context = context

    def _test_route(self, route, expected_status_code):
        response = self.server.get(route)
        self.assertEqual(response.status_code, expected_status_code)

    @wrappers.Request.application
    def _foo_handler(self, request):
        return wrappers.Response(response="hello world", status=200)

    def _bar_handler(self):
        pass

    @wrappers.Request.application
    def _eid_handler(self, request):
        eid = plugin_util.experiment_id(request.environ)
        body = json.dumps({"experiment_id": eid})
        return wrappers.Response(body, 200, content_type="application/json")

    @wrappers.Request.application
    def _wildcard_handler(self, request):
        if request.path == "/data/plugin/bar/wildcard/ok":
            return wrappers.Response(response="hello world", status=200)
        elif request.path == "/data/plugin/bar/wildcard/":
            # this route cannot actually be hit; see testEmptyWildcardRouteWithSlash.
            return wrappers.Response(response="hello world", status=200)
        else:
            return wrappers.Response(status=401)

    @wrappers.Request.application
    def _wildcard_special_handler(self, request):
        return wrappers.Response(status=300)

    def testPluginsAdded(self):
        # The routes are prefixed with /data/plugin/[plugin name].
        expected_routes = frozenset(
            ("/data/plugin/foo/foo_route", "/data/plugin/bar/bar_route",)
        )
        self.assertLessEqual(expected_routes, frozenset(self.app.exact_routes))

    def testNameToPluginMapping(self):
        # The mapping from plugin name to instance should include all plugins.
        mapping = self.context.plugin_name_to_instance
        self.assertItemsEqual(["foo", "bar", "whoami"], list(mapping.keys()))
        self.assertEqual("foo", mapping["foo"].plugin_name)
        self.assertEqual("bar", mapping["bar"].plugin_name)
        self.assertEqual("whoami", mapping["whoami"].plugin_name)

    def testNormalRoute(self):
        self._test_route("/data/plugin/foo/foo_route", 200)

    def testNormalRouteIsNotWildcard(self):
        self._test_route("/data/plugin/foo/foo_route/bogus", 404)

    def testMissingRoute(self):
        self._test_route("/data/plugin/foo/bogus", 404)

    def testExperimentIdIntegration_withNoExperimentId(self):
        response = self.server.get("/data/plugin/whoami/eid")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.get_data().decode("utf-8"))
        self.assertEqual(data, {"experiment_id": ""})

    def testExperimentIdIntegration_withExperimentId(self):
        response = self.server.get("/experiment/123/data/plugin/whoami/eid")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.get_data().decode("utf-8"))
        self.assertEqual(data, {"experiment_id": "123"})

    def testEmptyRoute(self):
        self._test_route("", 301)

    def testSlashlessRoute(self):
        self._test_route("runaway", 404)

    def testWildcardAcceptedRoute(self):
        self._test_route("/data/plugin/bar/wildcard/ok", 200)

    def testLongerWildcardRouteTakesPrecedence(self):
        # This tests that the longer 'special' wildcard takes precedence over
        # the shorter one.
        self._test_route("/data/plugin/bar/wildcard/special/blah", 300)

    def testExactRouteTakesPrecedence(self):
        # This tests that an exact match takes precedence over a wildcard.
        self._test_route("/data/plugin/bar/wildcard/special/exact", 200)

    def testWildcardRejectedRoute(self):
        # A plugin may reject a request passed to it via a wildcard route.
        # Note our test plugin returns 401 in this case, to distinguish this
        # response from a 404 passed if the route were not found.
        self._test_route("/data/plugin/bar/wildcard/bogus", 401)

    def testWildcardRouteWithoutSlash(self):
        # A wildcard route requires a slash before the '*'.
        # Lacking one, no route is matched.
        self._test_route("/data/plugin/bar/wildcard", 404)

    def testEmptyWildcardRouteWithSlash(self):
        # A wildcard route requires a slash before the '*'.  Here we include the
        # slash, so we might expect the route to match.
        #
        # However: Trailing slashes are automatically removed from incoming requests
        # in _clean_path().  Consequently, this request does not match the wildcard
        # route after all.
        #
        # Note the test plugin specifically accepts this route (returning 200), so
        # the fact that 404 is returned demonstrates that the plugin was not
        # consulted.
        self._test_route("/data/plugin/bar/wildcard/", 404)


if __name__ == "__main__":
    tb_test.main()
