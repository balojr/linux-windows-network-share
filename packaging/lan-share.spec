%global debug_package %{nil}
Name:           lan-share
Version:        0.1.0
Release:        1
Summary:        Show this machine in Windows' Network tab and mount Windows shares
License:        MIT
BuildArch:      noarch
Source0:        %{name}-%{version}.tar.gz
Requires:       python3 >= 3.9
Recommends:     samba
Recommends:     avahi
Recommends:     cifs-utils
Recommends:     polkit
# GUI (optional). One rpm serves Fedora/RHEL and openSUSE, whose package names differ, hence rich "or" dependencies.
Recommends:     (python3-gobject or python313-gobject or python314-gobject)
Recommends:     (libadwaita or typelib-1_0-Adw-1)
Recommends:     (gtk4 or typelib-1_0-Gtk-4_0)

%description
lan-share configures Samba, WS-Discovery (bundled wsdd), the firewall and SELinux
so a Linux machine appears in Windows Explorer's Network tab, and mounts Windows
shares on demand. Run "sudo lan-share setup" (dry run) then "--apply".

%prep
%setup -q

%install
DESTDIR=%{buildroot} PREFIX=/usr ./packaging/install.sh install

%files
%{_bindir}/lan-share
%{_datadir}/applications/io.github.lan_share.desktop
%{_datadir}/polkit-1/actions/io.github.lan_share.policy
%{_prefix}/lib/lan-share
%doc %{_docdir}/lan-share
%license LICENSE

%changelog
* Thu Nov 05 2026 lan-share contributors <noreply@example.invalid> - 0.1.0-1
- Initial package
