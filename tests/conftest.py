import libvirt


def make_libvirt_error(code):
    """指定したエラーコードを持つ libvirt.libvirtError を作る。

    MagicMock は BaseException ではなく raise できないため、実インスタンスを
    作って get_error_code だけ差し替える。
    """
    err = libvirt.libvirtError("mock error")
    err.get_error_code = lambda: code
    return err
