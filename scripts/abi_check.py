#!/usr/bin/env python3
"""This script compares the interfaces of two versions of Mbed TLS, looking
for backward incompatibilities between two different Git revisions within
an Mbed TLS repository. It must be run from the root of a Git working tree.

### How the script works ###

For the source (API) and runtime (ABI) interface compatibility, this script
is a small wrapper around the abi-compliance-checker and abi-dumper tools,
applying them to compare the header and library files.

For the storage format, this script compares the automatically generated
storage tests and the manual read tests, and complains if there is a
reduction in coverage. A change in test data will be signaled as a
coverage reduction since the old test data is no longer present. A change in
how test data is presented will be signaled as well; this would be a false
positive.

The results of the API/ABI comparison are either formatted as HTML and stored
at a configurable location, or are given as a brief list of problems.
Returns 0 on success, 1 on non-compliance, and 2 if there is an error
while running the script.

### How to interpret non-compliance ###

This script has relatively common false positives. In many scenarios, it only
reports a pass if there is a strict textual match between the old version and
the new version, and it reports problems where there is a sufficient semantic
match but not a textual match. This section lists some common false positives.
This is not an exhaustive list: in the end what matters is whether we are
breaking a backward compatibility goal.

**API**: the goal is that if an application works with the old version of the
library, it can be recompiled against the new version and will still work.
This is normally validated by comparing the declarations in `include/*/*.h`.
A failure is a declaration that has disappeared or that now has a different
type.

  * It's ok to change or remove macros and functions that are documented as
    for internal use only or as experimental.
  * It's ok to rename function or macro parameters as long as the semantics
    has not changed.
  * It's ok to change or remove structure fields that are documented as
    private.
  * It's ok to add fields to a structure that already had private fields
    or was documented as extensible.

**ABI**: the goal is that if an application was built against the old version
of the library, the same binary will work when linked against the new version.
This is normally validated by comparing the symbols exported by `libmbed*.so`.
A failure is a symbol that is no longer exported by the same library or that
now has a different type.

  * All ABI changes are acceptable if the library version is bumped
    (see `scripts/bump_version.sh`).
  * ABI changes that concern functions which are declared only inside the
    library directory, and not in `include/*/*.h`, are acceptable only if
    the function was only ever used inside the same library (libmbedcrypto,
    libmbedx509, libmbedtls). As a counter example, if the old version
    of libmbedtls calls mbedtls_foo() from libmbedcrypto, and the new version
    of libmbedcrypto no longer has a compatible mbedtls_foo(), this does
    require a version bump for libmbedcrypto.

**Storage format**: the goal is to check that persistent keys stored by the
old version can be read by the new version. This is normally validated by
comparing the `*read*` test cases in `test_suite*storage_format*.data`.
A failure is a storage read test case that is no longer present with the same
function name and parameter list.

  * It's ok if the same test data is present, but its presentation has changed,
    for example if a test function is renamed or has different parameters.
  * It's ok if redundant tests are removed.

**Generated test coverage**: the goal is to check that automatically
generated tests have as much coverage as before. This is normally validated
by comparing the test cases that are automatically generated by a script.
A failure is a generated test case that is no longer present with the same
function name and parameter list.

  * It's ok if the same test data is present, but its presentation has changed,
    for example if a test function is renamed or has different parameters.
  * It's ok if redundant tests are removed.

"""

# Copyright The Mbed TLS Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import glob
import os
import re
import sys
import traceback
import shutil
import subprocess
import argparse
import logging
import tempfile
import fnmatch
from types import SimpleNamespace

import xml.etree.ElementTree as ET

from mbedtls_dev import build_tree


class AbiChecker:
    """API and ABI checker."""

    def __init__(self, old_version, new_version, configuration):
        """Instantiate the API/ABI checker.

        old_version: RepoVersion containing details to compare against
        new_version: RepoVersion containing details to check
        configuration.report_dir: directory for output files
        configuration.keep_all_reports: if false, delete old reports
        configuration.brief: if true, output shorter report to stdout
        configuration.check_abi: if true, compare ABIs
        configuration.check_api: if true, compare APIs
        configuration.check_storage: if true, compare storage format tests
        configuration.skip_file: path to file containing symbols and types to skip
        """
        self.repo_path = "."
        self.log = None
        self.verbose = configuration.verbose
        self._setup_logger()
        self.report_dir = os.path.abspath(configuration.report_dir)
        self.keep_all_reports = configuration.keep_all_reports
        self.can_remove_report_dir = not (os.path.exists(self.report_dir) or
                                          self.keep_all_reports)
        self.old_version = old_version
        self.new_version = new_version
        self.skip_file = configuration.skip_file
        self.check_abi = configuration.check_abi
        self.check_api = configuration.check_api
        if self.check_abi != self.check_api:
            raise Exception('Checking API without ABI or vice versa is not supported')
        self.check_storage_tests = configuration.check_storage
        self.brief = configuration.brief
        self.git_command = "git"
        self.make_command = "make"

    def _setup_logger(self):
        self.log = logging.getLogger()
        if self.verbose:
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.INFO)
        self.log.addHandler(logging.StreamHandler())

    @staticmethod
    def check_abi_tools_are_installed():
        for command in ["abi-dumper", "abi-compliance-checker"]:
            if not shutil.which(command):
                raise Exception(f"{command} not installed, aborting")

    def _get_clean_worktree_for_git_revision(self, version):
        """Make a separate worktree with version.revision checked out.
        Do not modify the current worktree."""
        git_worktree_path = tempfile.mkdtemp()
        if version.repository:
            self.log.debug(
                f"Checking out git worktree for revision {version.revision} from {version.repository}"
            )
            fetch_output = subprocess.check_output(
                [self.git_command, "fetch",
                 version.repository, version.revision],
                cwd=self.repo_path,
                stderr=subprocess.STDOUT
            )
            self.log.debug(fetch_output.decode("utf-8"))
            worktree_rev = "FETCH_HEAD"
        else:
            self.log.debug(f"Checking out git worktree for revision {version.revision}")
            worktree_rev = version.revision
        worktree_output = subprocess.check_output(
            [self.git_command, "worktree", "add", "--detach",
             git_worktree_path, worktree_rev],
            cwd=self.repo_path,
            stderr=subprocess.STDOUT
        )
        self.log.debug(worktree_output.decode("utf-8"))
        version.commit = subprocess.check_output(
            [self.git_command, "rev-parse", "HEAD"],
            cwd=git_worktree_path,
            stderr=subprocess.STDOUT
        ).decode("ascii").rstrip()
        self.log.debug(f"Commit is {version.commit}")
        return git_worktree_path

    def _update_git_submodules(self, git_worktree_path, version):
        """If the crypto submodule is present, initialize it.
        if version.crypto_revision exists, update it to that revision,
        otherwise update it to the default revision"""
        update_output = subprocess.check_output(
            [self.git_command, "submodule", "update", "--init", '--recursive'],
            cwd=git_worktree_path,
            stderr=subprocess.STDOUT
        )
        self.log.debug(update_output.decode("utf-8"))
        if not (os.path.exists(os.path.join(git_worktree_path, "crypto"))
                and version.crypto_revision):
            return

        if version.crypto_repository:
            fetch_output = subprocess.check_output(
                [self.git_command, "fetch", version.crypto_repository,
                 version.crypto_revision],
                cwd=os.path.join(git_worktree_path, "crypto"),
                stderr=subprocess.STDOUT
            )
            self.log.debug(fetch_output.decode("utf-8"))
            crypto_rev = "FETCH_HEAD"
        else:
            crypto_rev = version.crypto_revision

        checkout_output = subprocess.check_output(
            [self.git_command, "checkout", crypto_rev],
            cwd=os.path.join(git_worktree_path, "crypto"),
            stderr=subprocess.STDOUT
        )
        self.log.debug(checkout_output.decode("utf-8"))

    def _build_shared_libraries(self, git_worktree_path, version):
        """Build the shared libraries in the specified worktree."""
        my_environment = os.environ.copy()
        my_environment["CFLAGS"] = "-g -Og"
        my_environment["SHARED"] = "1"
        if os.path.exists(os.path.join(git_worktree_path, "crypto")):
            my_environment["USE_CRYPTO_SUBMODULE"] = "1"
        make_output = subprocess.check_output(
            [self.make_command, "lib"],
            env=my_environment,
            cwd=git_worktree_path,
            stderr=subprocess.STDOUT
        )
        self.log.debug(make_output.decode("utf-8"))
        for root, _dirs, files in os.walk(git_worktree_path):
            for file in fnmatch.filter(files, "*.so"):
                version.modules[os.path.splitext(file)[0]] = (
                    os.path.join(root, file)
                )

    @staticmethod
    def _pretty_revision(version):
        if version.revision == version.commit:
            return version.revision
        else:
            return f"{version.revision} ({version.commit})"

    def _get_abi_dumps_from_shared_libraries(self, version):
        """Generate the ABI dumps for the specified git revision.
        The shared libraries must have been built and the module paths
        present in version.modules."""
        for mbed_module, module_path in version.modules.items():
            output_path = os.path.join(
                self.report_dir,
                f"{mbed_module}-{version.revision}-{version.version}.dump",
            )
            abi_dump_command = [
                "abi-dumper",
                module_path,
                "-o", output_path,
                "-lver", self._pretty_revision(version),
            ]
            abi_dump_output = subprocess.check_output(
                abi_dump_command,
                stderr=subprocess.STDOUT
            )
            self.log.debug(abi_dump_output.decode("utf-8"))
            version.abi_dumps[mbed_module] = output_path

    @staticmethod
    def _normalize_storage_test_case_data(line):
        """Eliminate cosmetic or irrelevant details in storage format test cases."""
        line = re.sub(r'\s+', r'', line)
        return line

    def _read_storage_tests(self,
                            directory,
                            filename,
                            is_generated,
                            storage_tests):
        """Record storage tests from the given file.

        Populate the storage_tests dictionary with test cases read from
        filename under directory.
        """
        at_paragraph_start = True
        description = None
        full_path = os.path.join(directory, filename)
        with open(full_path) as fd:
            for line_number, line in enumerate(fd, 1):
                line = line.strip()
                if not line:
                    at_paragraph_start = True
                    continue
                if line.startswith('#'):
                    continue
                if at_paragraph_start:
                    description = line.strip()
                    at_paragraph_start = False
                    continue
                if line.startswith('depends_on:'):
                    continue
                # We've reached a test case data line
                test_case_data = self._normalize_storage_test_case_data(line)
                if not is_generated:
                    # In manual test data, only look at read tests.
                    function_name = test_case_data.split(':', 1)[0]
                    if 'read' not in function_name.split('_'):
                        continue
                metadata = SimpleNamespace(
                    filename=filename,
                    line_number=line_number,
                    description=description
                )
                storage_tests[test_case_data] = metadata

    @staticmethod
    def _list_generated_test_data_files(git_worktree_path):
        """List the generated test data files."""
        output = subprocess.check_output(
            ['tests/scripts/generate_psa_tests.py', '--list'],
            cwd=git_worktree_path,
        ).decode('ascii')
        return [line for line in output.split('\n') if line]

    def _get_storage_format_tests(self, version, git_worktree_path):
        """Record the storage format tests for the specified git version.

        The storage format tests are the test suite data files whose name
        contains "storage_format".

        The version must be checked out at git_worktree_path.

        This function creates or updates the generated data files.
        """
        # Existing test data files. This may be missing some automatically
        # generated files if they haven't been generated yet.
        storage_data_files = set(glob.glob(
            'tests/suites/test_suite_*storage_format*.data'
        ))
        # Discover and (re)generate automatically generated data files.
        to_be_generated = set()
        for filename in self._list_generated_test_data_files(git_worktree_path):
            if 'storage_format' in filename:
                storage_data_files.add(filename)
                to_be_generated.add(filename)
        subprocess.check_call(
            ['tests/scripts/generate_psa_tests.py'] + sorted(to_be_generated),
            cwd=git_worktree_path,
        )
        for test_file in sorted(storage_data_files):
            self._read_storage_tests(git_worktree_path,
                                     test_file,
                                     test_file in to_be_generated,
                                     version.storage_tests)

    def _cleanup_worktree(self, git_worktree_path):
        """Remove the specified git worktree."""
        shutil.rmtree(git_worktree_path)
        worktree_output = subprocess.check_output(
            [self.git_command, "worktree", "prune"],
            cwd=self.repo_path,
            stderr=subprocess.STDOUT
        )
        self.log.debug(worktree_output.decode("utf-8"))

    def _get_abi_dump_for_ref(self, version):
        """Generate the interface information for the specified git revision."""
        git_worktree_path = self._get_clean_worktree_for_git_revision(version)
        self._update_git_submodules(git_worktree_path, version)
        if self.check_abi:
            self._build_shared_libraries(git_worktree_path, version)
            self._get_abi_dumps_from_shared_libraries(version)
        if self.check_storage_tests:
            self._get_storage_format_tests(version, git_worktree_path)
        self._cleanup_worktree(git_worktree_path)

    def _remove_children_with_tag(self, parent, tag):
        children = parent.getchildren()
        for child in children:
            if child.tag == tag:
                parent.remove(child)
            else:
                self._remove_children_with_tag(child, tag)

    def _remove_extra_detail_from_report(self, report_root):
        for tag in ['test_info', 'test_results', 'problem_summary',
                    'added_symbols', 'affected']:
            self._remove_children_with_tag(report_root, tag)

        for report in report_root:
            for problems in report.getchildren()[:]:
                if not problems.getchildren():
                    report.remove(problems)

    def _abi_compliance_command(self, mbed_module, output_path):
        """Build the command to run to analyze the library mbed_module.
        The report will be placed in output_path."""
        abi_compliance_command = [
            "abi-compliance-checker",
            "-l", mbed_module,
            "-old", self.old_version.abi_dumps[mbed_module],
            "-new", self.new_version.abi_dumps[mbed_module],
            "-strict",
            "-report-path", output_path,
        ]
        if self.skip_file:
            abi_compliance_command += ["-skip-symbols", self.skip_file,
                                       "-skip-types", self.skip_file]
        if self.brief:
            abi_compliance_command += ["-report-format", "xml",
                                       "-stdout"]
        return abi_compliance_command

    def _is_library_compatible(self, mbed_module, compatibility_report):
        """Test if the library mbed_module has remained compatible.
        Append a message regarding compatibility to compatibility_report."""
        output_path = os.path.join(
            self.report_dir,
            f"{mbed_module}-{self.old_version.revision}-{self.new_version.revision}.html",
        )
        try:
            subprocess.check_output(
                self._abi_compliance_command(mbed_module, output_path),
                stderr=subprocess.STDOUT
            )
        except subprocess.CalledProcessError as err:
            if err.returncode != 1:
                raise err
            if self.brief:
                self.log.info(f"Compatibility issues found for {mbed_module}")
                report_root = ET.fromstring(err.output.decode("utf-8"))
                self._remove_extra_detail_from_report(report_root)
                self.log.info(ET.tostring(report_root).decode("utf-8"))
            else:
                self.can_remove_report_dir = False
                compatibility_report.append(
                    f"Compatibility issues found for {mbed_module}, for details see {output_path}"
                )
            return False
        compatibility_report.append(f"No compatibility issues for {mbed_module}")
        if not (self.keep_all_reports or self.brief):
            os.remove(output_path)
        return True

    @staticmethod
    def _is_storage_format_compatible(old_tests, new_tests,
                                      compatibility_report):
        """Check whether all tests present in old_tests are also in new_tests.

        Append a message regarding compatibility to compatibility_report.
        """
        missing = frozenset(old_tests.keys()).difference(new_tests.keys())
        for test_data in sorted(missing):
            metadata = old_tests[test_data]
            compatibility_report.append(
                f'Test case from {metadata.filename} line {metadata.line_number} "{metadata.description}" has disappeared: {test_data}'
            )
        compatibility_report.append(
            f'FAIL: {len(missing)}/{len(old_tests)} storage format test cases have changed or disappeared.'
            if missing
            else f'PASS: All {len(old_tests)} storage format test cases are preserved.'
        )
        compatibility_report.append(
            f'Info: number of storage format tests cases: {len(old_tests)} -> {len(new_tests)}.'
        )
        return not missing

    def get_abi_compatibility_report(self):
        """Generate a report of the differences between the reference ABI
        and the new ABI. ABI dumps from self.old_version and self.new_version
        must be available."""
        compatibility_report = [
            f"Checking evolution from {self._pretty_revision(self.old_version)} to {self._pretty_revision(self.new_version)}"
        ]
        compliance_return_code = 0

        if self.check_abi:
            shared_modules = list(set(self.old_version.modules.keys()) &
                                  set(self.new_version.modules.keys()))
            for mbed_module in shared_modules:
                if not self._is_library_compatible(mbed_module,
                                                   compatibility_report):
                    compliance_return_code = 1

        if self.check_storage_tests and not self._is_storage_format_compatible(
            self.old_version.storage_tests,
            self.new_version.storage_tests,
            compatibility_report,
        ):
            compliance_return_code = 1

        for version in [self.old_version, self.new_version]:
            for mbed_module, mbed_module_dump in version.abi_dumps.items():
                os.remove(mbed_module_dump)
        if self.can_remove_report_dir:
            os.rmdir(self.report_dir)
        self.log.info("\n".join(compatibility_report))
        return compliance_return_code

    def check_for_abi_changes(self):
        """Generate a report of ABI differences
        between self.old_rev and self.new_rev."""
        build_tree.check_repo_path()
        if self.check_api or self.check_abi:
            self.check_abi_tools_are_installed()
        self._get_abi_dump_for_ref(self.old_version)
        self._get_abi_dump_for_ref(self.new_version)
        return self.get_abi_compatibility_report()


def run_main():
    try:
        parser = argparse.ArgumentParser(
            description=__doc__
        )
        parser.add_argument(
            "-v", "--verbose", action="store_true",
            help="set verbosity level",
        )
        parser.add_argument(
            "-r", "--report-dir", type=str, default="reports",
            help="directory where reports are stored, default is reports",
        )
        parser.add_argument(
            "-k", "--keep-all-reports", action="store_true",
            help="keep all reports, even if there are no compatibility issues",
        )
        parser.add_argument(
            "-o", "--old-rev", type=str, help="revision for old version.",
            required=True,
        )
        parser.add_argument(
            "-or", "--old-repo", type=str, help="repository for old version."
        )
        parser.add_argument(
            "-oc", "--old-crypto-rev", type=str,
            help="revision for old crypto submodule."
        )
        parser.add_argument(
            "-ocr", "--old-crypto-repo", type=str,
            help="repository for old crypto submodule."
        )
        parser.add_argument(
            "-n", "--new-rev", type=str, help="revision for new version",
            required=True,
        )
        parser.add_argument(
            "-nr", "--new-repo", type=str, help="repository for new version."
        )
        parser.add_argument(
            "-nc", "--new-crypto-rev", type=str,
            help="revision for new crypto version"
        )
        parser.add_argument(
            "-ncr", "--new-crypto-repo", type=str,
            help="repository for new crypto submodule."
        )
        parser.add_argument(
            "-s", "--skip-file", type=str,
            help=("path to file containing symbols and types to skip "
                  "(typically \"-s identifiers\" after running "
                  "\"tests/scripts/list-identifiers.sh --internal\")")
        )
        parser.add_argument(
            "--check-abi",
            action='store_true', default=True,
            help="Perform ABI comparison (default: yes)"
        )
        parser.add_argument("--no-check-abi", action='store_false', dest='check_abi')
        parser.add_argument(
            "--check-api",
            action='store_true', default=True,
            help="Perform API comparison (default: yes)"
        )
        parser.add_argument("--no-check-api", action='store_false', dest='check_api')
        parser.add_argument(
            "--check-storage",
            action='store_true', default=True,
            help="Perform storage tests comparison (default: yes)"
        )
        parser.add_argument("--no-check-storage", action='store_false', dest='check_storage')
        parser.add_argument(
            "-b", "--brief", action="store_true",
            help="output only the list of issues to stdout, instead of a full report",
        )
        abi_args = parser.parse_args()
        if os.path.isfile(abi_args.report_dir):
            print(f"Error: {abi_args.report_dir} is not a directory")
            parser.exit()
        old_version = SimpleNamespace(
            version="old",
            repository=abi_args.old_repo,
            revision=abi_args.old_rev,
            commit=None,
            crypto_repository=abi_args.old_crypto_repo,
            crypto_revision=abi_args.old_crypto_rev,
            abi_dumps={},
            storage_tests={},
            modules={}
        )
        new_version = SimpleNamespace(
            version="new",
            repository=abi_args.new_repo,
            revision=abi_args.new_rev,
            commit=None,
            crypto_repository=abi_args.new_crypto_repo,
            crypto_revision=abi_args.new_crypto_rev,
            abi_dumps={},
            storage_tests={},
            modules={}
        )
        configuration = SimpleNamespace(
            verbose=abi_args.verbose,
            report_dir=abi_args.report_dir,
            keep_all_reports=abi_args.keep_all_reports,
            brief=abi_args.brief,
            check_abi=abi_args.check_abi,
            check_api=abi_args.check_api,
            check_storage=abi_args.check_storage,
            skip_file=abi_args.skip_file
        )
        abi_check = AbiChecker(old_version, new_version, configuration)
        return_code = abi_check.check_for_abi_changes()
        sys.exit(return_code)
    except Exception: # pylint: disable=broad-except
        # Print the backtrace and exit explicitly so as to exit with
        # status 2, not 1.
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    run_main()
