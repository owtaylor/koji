# kojid plugin

import commands
import koji
import ConfigParser
import os
import platform
compat_mode = False
try:
    import koji.tasks as tasks
    from koji.tasks import scan_mounts
    from koji.util import isSuccess as _isSuccess
    from koji.util import parseStatus as _parseStatus
    from koji.daemon import log_output
    from __main__ import BuildRoot
except ImportError:
    compat_mode = True
    #old way
    import tasks
    #XXX - stuff we need from kojid
    from __main__ import BuildRoot, log_output, scan_mounts, _isSuccess, _parseStatus


__all__ = ('RunRootTask',)

CONFIG_FILE = '/etc/kojid/plugins/runroot.conf'


class RunRootTask(tasks.BaseTaskHandler):

    Methods = ['runroot']

    _taskWeight = 2.0

    def __init__(self, *args, **kwargs):
        self._read_config()
        return super(RunRootTask, self).__init__(*args, **kwargs)

    def _get_path_params(self, path, rw=False):
        found = False
        for mount_data in self.config['paths']:
            if path.startswith(mount_data['mountpoint']):
                found = True
                break
        if not found:
            raise koji.GenericError("bad config: missing corresponding mountpoint")
        options = []
        for o in mount_data['options'].split(','):
            if rw and o == 'ro':
                options.append('rw')
            else:
                options.append(o)
        rel_path = path[len(mount_data['mountpoint']):]
        rel_path = rel_path[1:] if rel_path.startswith('/') else rel_path
        res = (os.path.join(mount_data['path'], rel_path), path, mount_data['fstype'], ','.join(options))
        return res

    def _read_config(self):
        cp = ConfigParser.SafeConfigParser()
        cp.read(CONFIG_FILE)
        self.config = {
           'default_mounts': [],
           'safe_roots': [],
           'path_subs': [],
           'paths': [],
        }

        if cp.has_option('paths', 'default_mounts'):
            self.config['default_mounts'] = cp.get('paths', 'default_mounts').split(',')
        if cp.has_option('paths', 'safe_roots'):
            self.config['safe_roots'] = cp.get('paths', 'safe_roots').split(',')
        if cp.has_option('paths', 'path_subs'):
            self.config['path_subs'] = [x.split(',') for x in cp.get('paths', 'path_subs').split('\n')]

        count = 0
        while True:
            section_name = 'path%d' % count
            if not cp.has_section(section_name):
                break
            try:
                self.config['paths'].append({
                    'mountpoint': cp.get(section_name, 'mountpoint'),
                    'path': cp.get(section_name, 'path'),
                    'fstype': cp.get(section_name, 'fstype'),
                    'options': cp.get(section_name, 'options'),
                })
            except ConfigParser.NoOptionError:
                raise koji.GenericError("bad config: missing options in %s section" % section_name)
            count += 1

        for path in self.config['default_mounts'] + self.config['safe_roots'] + [x[0] for x in self.config['path_subs']]:
            if not path.startswith('/'):
                raise koji.GenericError("bad config: all paths (default_mounts, safe_roots, path_subs) needs to be absolute: %s" % path)

    def handler(self, root, arch, command, keep=False, packages=[], mounts=[], repo_id=None, skip_setarch=False, weight=None, upload_logs=None):
        """Create a buildroot and run a command (as root) inside of it

        Command may be a string or a list.

        Returns a message indicating success if the command was successful, and
        raises an error otherwise.  Command output will be available in
        runroot.log in the task output directory on the hub.

        skip_setarch is a rough approximation of an old hack

        the keep option is not used. keeping for compatibility for now...

        upload_logs is list of absolute paths which will be uploaded for
        archiving on hub. It always consists of /tmp/runroot.log, but can be
        used for additional logs (pungi.log, etc.)
        """
        if weight is not None:
            weight = max(weight, 0.5)
            self.session.host.setTaskWeight(self.id, weight)
        #noarch is funny
        if arch == "noarch":
            #we need a buildroot arch. Pick one that:
            #  a) this host can handle
            #  b) the build tag can support
            #  c) is canonical
            host_arches = self.session.host.getHost()['arches']
            if not host_arches:
                raise koji.BuildError, "No arch list for this host"
            tag_arches = self.session.getBuildConfig(root)['arches']
            if not tag_arches:
                raise koji.BuildError, "No arch list for tag: %s" % root
            #index canonical host arches
            host_arches = dict([(koji.canonArch(a),1) for a in host_arches.split()])
            #pick the first suitable match from tag's archlist
            for br_arch in tag_arches.split():
                br_arch = koji.canonArch(br_arch)
                if host_arches.has_key(br_arch):
                    #we're done
                    break
            else:
                #no overlap
                raise koji.BuildError, "host does not match tag arches: %s (%s)" % (root, tag_arches)
        else:
            br_arch = arch
        if repo_id:
            repo_info = self.session.repoInfo(repo_id, strict=True)
            if repo_info['tag_name'] != root:
                raise koji.BuildError, "build tag (%s) does not match repo tag (%s)" % (root, repo_info['tag_name'])
            if repo_info['state'] not in (koji.REPO_STATES['READY'], koji.REPO_STATES['EXPIRED']):
                raise koji.BuildError, "repos in the %s state may not be used by runroot" % koji.REPO_STATES[repo_info['state']]
        else:
            repo_info = self.session.getRepo(root)
        if not repo_info:
            #wait for it
            task_id = self.session.host.subtask(method='waitrepo',
                                           arglist=[root, None, None],
                                           parent=self.id)
            repo_info = self.wait(task_id)[task_id]
        if compat_mode:
            broot = BuildRoot(root, br_arch, self.id, repo_id=repo_info['id'])
        else:
            broot = BuildRoot(self.session, self.options, root, br_arch, self.id, repo_id=repo_info['id'])
            broot.workdir = self.workdir
        broot.init()
        rootdir = broot.rootdir()
        #workaround for rpm oddness
        os.system('rm -f "%s"/var/lib/rpm/__db.*' % rootdir)
        #update buildroot state (so that updateBuildRootList() will work)
        self.session.host.setBuildRootState(broot.id, 'BUILDING')
        try:
            if packages:
                #pkglog = '%s/%s' % (broot.resultdir(), 'packages.log')
                pkgcmd = ['--install'] + packages
                status = broot.mock(pkgcmd)
                self.session.host.updateBuildRootList(broot.id, broot.getPackageList())
                if not _isSuccess(status):
                    raise koji.BuildrootError, _parseStatus(status, pkgcmd)

            if isinstance(command, str):
                cmdstr = command
            else:
                #we were passed an arglist
                #we still have to run this through the shell (for redirection)
                #but we can preserve the list structure precisely with careful escaping
                cmdstr = ' '.join(["'%s'" % arg.replace("'", r"'\''") for arg in command])
            # A nasty hack to put command output into its own file until mock can be
            # patched to do something more reasonable than stuff everything into build.log
            cmdargs = ['/bin/sh', '-c', "{ %s; } < /dev/null 2>&1 | /usr/bin/tee /tmp/runroot.log; exit ${PIPESTATUS[0]}" % cmdstr]

            # always mount /mnt/redhat (read-only)
            # always mount /mnt/iso (read-only)
            # also need /dev bind mount
            self.do_mounts(rootdir, [self._get_path_params(x) for x in self.config['default_mounts']])
            self.do_extra_mounts(rootdir, mounts)
            mock_cmd = ['chroot']
            if skip_setarch:
                #we can't really skip it, but we can set it to the current one instead of of the chroot one
                myarch = platform.uname()[5]
                mock_cmd.extend(['--arch', myarch])
            mock_cmd.append('--')
            mock_cmd.extend(cmdargs)
            rv = broot.mock(mock_cmd)
            log_paths = ['/tmp/runroot.log']
            if upload_logs is not None:
                log_paths += upload_logs
            for log_path in log_paths:
                self.uploadFile(rootdir + log_path)
        finally:
            # mock should umount its mounts, but it will not handle ours
            self.undo_mounts(rootdir, fatal=False)
            broot.expire()
        if isinstance(command, str):
            cmdlist = command.split()
        else:
            cmdlist = command
        cmdlist = [param for param in cmdlist if '=' not in param]
        if cmdlist:
            cmd = os.path.basename(cmdlist[0])
        else:
            cmd = '(none)'
        if _isSuccess(rv):
            return '%s completed successfully' % cmd
        else:
            raise koji.BuildrootError, _parseStatus(rv, cmd)

    def do_extra_mounts(self, rootdir, mounts):
        mnts = []
        for mount in mounts:
            mount = os.path.normpath(mount)
            for safe_root in self.config['safe_roots']:
                if mount.startswith(safe_root):
                    break
            else:
                #no match
                raise koji.GenericError("read-write mount point is not safe: %s" % mount)
            #normpath should have removed any .. dirs, but just in case...
            if mount.find('/../') != -1:
                raise koji.GenericError("read-write mount point is not safe: %s" % mount)

            for re, sub in self.config['path_subs']:
                mount = mount.replace(re, sub)

            mnts.append(self._get_path_params(mount, rw=True))
        self.do_mounts(rootdir, mnts)

    def do_mounts(self, rootdir, mounts):
        if not mounts:
            return
        self.logger.info('New runroot')
        self.logger.info("Runroot mounts: %s" % mounts)
        fn = '%s/tmp/runroot_mounts' % rootdir
        fslog = file(fn, 'a')
        logfile = "%s/do_mounts.log" % self.workdir
        uploadpath = self.getUploadDir()
        error = None
        for dev,path,type,opts in mounts:
            if not path.startswith('/'):
                raise koji.GenericError("invalid mount point: %s" % path)
            mpoint = "%s%s" % (rootdir,path)
            if opts is None:
                opts = []
            else:
                opts = opts.split(',')
            if 'bind' in opts:
                #make sure dir exists
                if not os.path.isdir(dev):
                    error = koji.GenericError("No such directory or mount: %s" % dev)
                    break
                type = 'none'
                if path is None:
                    #shorthand for "same path"
                    path = dev
            if 'bg' in opts:
                error = koji.GenericError("bad config: background mount not allowed")
                break
            opts = ','.join(opts)
            cmd = ['mount', '-t', type, '-o', opts, dev, mpoint]
            self.logger.info("Mount command: %r" % cmd)
            koji.ensuredir(mpoint)
            if compat_mode:
                status = log_output(cmd[0], cmd, logfile, uploadpath, logerror=True, append=True)
            else:
                status = log_output(self.session, cmd[0], cmd, logfile, uploadpath, logerror=True, append=True)
            if not _isSuccess(status):
                error = koji.GenericError("Unable to mount %s: %s" \
                        % (mpoint, _parseStatus(status, cmd)))
                break
            fslog.write("%s\n" % mpoint)
            fslog.flush()
        fslog.close()
        if error is not None:
            self.undo_mounts(rootdir, fatal=False)
            raise error

    def undo_mounts(self, rootdir, fatal=True):
        self.logger.debug("Unmounting runroot mounts")
        mounts = {}
        fn = '%s/tmp/runroot_mounts' % rootdir
        if os.path.exists(fn):
            fslog = file(fn,'r')
            for line in fslog:
                mounts.setdefault(line.strip(), 1)
            fslog.close()
        #also, check /proc/mounts just in case
        for dir in scan_mounts(rootdir):
            mounts.setdefault(dir, 1)
        mounts = mounts.keys()
        # deeper directories first
        mounts.sort()
        mounts.reverse()
        failed = []
        self.logger.info("Unmounting (runroot): %s" % mounts)
        for dir in mounts:
            (rv, output) = commands.getstatusoutput("umount -l '%s'" % dir)
            if rv != 0:
                failed.append("%s: %s" % (dir, output))
        if failed:
            msg = "Unable to unmount: %s" % ', '.join(failed)
            self.logger.warn(msg)
            if fatal:
                raise koji.GenericError, msg
        else:
            # remove the mount list when everything is unmounted
            try:
                os.unlink(fn)
            except OSError:
                pass
