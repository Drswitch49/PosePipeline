
import os
import sys

class add_path():
    def __init__(self, path):
        if not isinstance(path, list):
            self.path = [path]
        else:
            self.path = path

    def __enter__(self):
        for p in self.path:
            sys.path.insert(0, p)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            for p in self.path:
                sys.path.remove(p)
        except ValueError:
            pass

def set_environmental_variables():
    # TODO: should create a cfg file or use a path relative to module for this instead
    # of hardcoding for my local setup
    os.environ['OPENPOSE_PATH'] = '/home/jcotton/projects/pose/openpose'
    os.environ['OPENPOSE_PYTHON_PATH'] = '/home/jcotton/projects/pose/openpose/build/python'
    os.environ['EXPOSE_PATH'] = '/home/jcotton/projects/pose/expose'
    os.environ['CENTERHMR_PATH'] = '/home/jcotton/projects/pose/CenterHMR'
    os.environ["GAST_PATH"] = '/home/jcotton/projects/pose/GAST-Net-3DPoseEstimation'
    os.environ["POSEFORMER_PATH"] = '/home/jcotton/projects/pose/PoseFormer'
    os.environ["VIBE_PATH"] = '/home/jcotton/projects/pose/VIBE'
    os.environ["MEVA_PATH"] = '/home/jcotton/projects/pose/MEVA'
    os.environ["FAIRMOT_PATH"] = '/home/jcotton/projects/pose/FairMOT/src/lib'
    os.environ["DCNv2_PATH"] = '/home/jcotton/projects/pose/DCNv2/DCN'

    import platform
    if 'Ubuntu' in platform.version():
        # In Ubuntu, using osmesa mode for rendering
        os.environ['PYOPENGL_PLATFORM'] = 'egl'

