(function () {
    // Toggle the alternate-email field on the vault form. No-ops on pages
    // without the form so this file is safe to include site-wide.
    function sync() {
        var other = document.getElementById('dest-other');
        var wrap = document.getElementById('other-email-wrap');
        if (!other || !wrap) return;
        var show = other.checked;
        wrap.style.display = show ? '' : 'none';
        var input = wrap.querySelector('input');
        if (input) input.required = show;
    }
    document.addEventListener('change', function (event) {
        if (event.target && event.target.name === 'destination') sync();
    });
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', sync);
    } else {
        sync();
    }
})();
