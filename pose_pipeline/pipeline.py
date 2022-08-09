
import os
import sys
import cv2
import tempfile
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import shutil

import datajoint as dj

from .utils.keypoint_matching import match_keypoints_to_bbox
from .env import add_path

if "custom" not in dj.config:
    dj.config["custom"] = {}

db_prefix = dj.config["custom"].get("database.prefix", "")

schema = dj.schema(db_prefix +'pose_pipeline')


@schema
class Video(dj.Manual):
    definition = '''
    video_project       : varchar(50)
    filename            : varchar(100)
    ---
    video               : attach@localattach    # datajoint managed video file
    start_time          : timestamp             # time of beginning of video, as accurately as known
    '''

    @staticmethod
    def make_entry(filepath, session_id=None):
        from datetime import datetime
        import os

        _, fn = os.path.split(filepath)
        date = datetime.strptime(fn[:16], '%Y%m%d-%H%M%SZ')
        d = {'filename': fn, 'video': filepath, 'start_time': date}
        if session_id is not None:
            d.update({'session_id': session_id})
        return d

    @staticmethod
    def get_robust_reader(key, return_cap=True):
        import subprocess
        import tempfile

        # fetch video and place in temp directory
        video = (Video & key).fetch1('video')
        fd, outfile = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)
        shutil.move(video,outfile)

        video = outfile

        cap = cv2.VideoCapture(video)

        # check all the frames are readable
        expected_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        completed = True

        def compress(video):
            fd, outfile = tempfile.mkstemp(suffix='.mp4')
            print(f'Unable to read all the fails. Transcoding {video} to {outfile}')
            subprocess.run(['ffmpeg', '-y', '-i', video, '-c:v', 'libx264', '-b:v', '1M', outfile])
            os.close(fd)
            return outfile

        for i in range(expected_frames):
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.release()

                video = compress(video)
                cap = cv2.VideoCapture(video)
                break

        if return_cap:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            return cap
        else:
            cap.release()
            return video


@schema
class VideoInfo(dj.Computed):
    definition = '''
    -> Video
    ---
    timestamps      : longblob
    delta_time      : longblob
    fps             : float
    height          : int
    width           : int
    num_frames      : int
    '''

    def make(self, key):

        video, start_time = (Video & key).fetch1('video', 'start_time')

        cap = cv2.VideoCapture(video)
        key['fps'] = fps = cap.get(cv2.CAP_PROP_FPS)
        key['num_frames'] = frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        key['width'] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        key['height'] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        key['timestamps'] = [start_time + timedelta(0, i / fps) for i in range(frames)]
        key['delta_time'] = [timedelta(0, i / fps).total_seconds() for i in range(frames)]
        self.insert1(key)

        cap.release()
        os.remove(video)

    def fetch_timestamps(self):
        assert len(self) == 1, "Restrict to single entity"
        timestamps = self.fetch1('timestamps')
        timestamps = np.array([(t-timestamps[0]).total_seconds() for t in timestamps])
        return timestamps


@schema
class BottomUpMethodLookup(dj.Lookup):
    definition = '''
    bottom_up_method_name : varchar(50)
    '''
    contents = [
        {'bottom_up_method_name': 'OpenPose'},
        {'bottom_up_method_name': 'MMPose'}]


@schema
class BottomUpMethod(dj.Manual):
    definition = '''
    -> Video
    -> BottomUpMethodLookup
    '''


@schema
class BottomUpPeople(dj.Computed):
    definition = '''
    -> BottomUpMethod
    ---
    keypoints                   : longblob
    timestamp=CURRENT_TIMESTAMP : timestamp    # automatic timestamp
    '''

    def make(self, key):

        if key['bottom_up_method_name'] == 'OpenPose':
            raise Exception('OpenPose wrapper not implemented yet')
        elif key['bottom_up_method_name'] == 'MMPose':
            from .wrappers.mmpose import mmpose_bottom_up
            key['keypoints'] = mmpose_bottom_up(key)
        else:
            raise Exception("Method not implemented")

        self.insert1(key)


@schema
class OpenPose(dj.Computed):
    definition = '''
    -> Video
    ---
    keypoints         : longblob
    pose_ids          : longblob
    pose_scores       : longblob
    face_keypoints    : longblob
    hand_keypoints    : longblob
    '''

    def make(self, key):

        video = Video.get_robust_reader(key, return_cap=False)

        with add_path(os.path.join(os.environ['OPENPOSE_PATH'], 'build/python')):
            from pose_pipeline.wrappers.openpose import openpose_parse_video
            res = openpose_parse_video(video)

        key['keypoints'] = [r['keypoints'] for r in res]
        key['pose_ids'] = [r['pose_ids'] for r in res]
        key['pose_scores'] = [r['pose_scores'] for r in res]
        key['hand_keypoints'] = [r['hand_keypoints'] for r in res]
        key['face_keypoints'] = [r['face_keypoints'] for r in res]

        self.insert1(key)

        # remove the downloaded video to avoid clutter
        os.remove(video)


@schema
class BlurredVideo(dj.Computed):
    definition = '''
    -> Video
    ---
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):

        from .wrappers.facenet import blur_faces

        blurred_video = blur_faces(key)

        key['output_video'] = blurred_video
        self.insert1(key)

        os.remove(blurred_video)


@schema
class TrackingBboxMethodLookup(dj.Lookup):
    definition = '''
    tracking_method      : int
    ---
    tracking_method_name : varchar(50)
    '''
    contents = [
        {'tracking_method': 0, 'tracking_method_name': 'DeepSortYOLOv4'},
        {'tracking_method': 1, 'tracking_method_name': 'MMTrack_tracktor'},
        {'tracking_method': 2, 'tracking_method_name': 'FairMOT'},
        {'tracking_method': 3, 'tracking_method_name': 'TransTrack'},
        {'tracking_method': 4, 'tracking_method_name': 'TraDeS'},
        {'tracking_method': 5, 'tracking_method_name': 'MMTrack_deepsort'},
        {'tracking_method': 6, 'tracking_method_name': 'MMTrack_bytetrack'}
    ]

@schema
class TrackingBboxMethod(dj.Manual):
    definition = '''
    -> Video
    tracking_method   : int
    ---
    '''

@schema
class TrackingBbox(dj.Computed):
    definition = '''
    -> TrackingBboxMethod
    ---
    tracks            : longblob
    num_tracks        : int
    '''

    def make(self, key):

        video = Video.get_robust_reader(key, return_cap=False)

        if (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') == 'DeepSortYOLOv4':
            from pose_pipeline.wrappers.deep_sort_yolov4.parser import tracking_bounding_boxes
            tracks = tracking_bounding_boxes(video)
            key['tracks'] = tracks

        elif (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') in 'MMTrack_tracktor':
            from pose_pipeline.wrappers.mmtrack import mmtrack_bounding_boxes
            tracks = mmtrack_bounding_boxes(video, 'tracktor')
            key['tracks'] = tracks

        elif (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') == 'MMTrack_deepsort':
            from pose_pipeline.wrappers.mmtrack import mmtrack_bounding_boxes
            tracks = mmtrack_bounding_boxes(video, 'deepsort')
            key['tracks'] = tracks

        elif (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') == 'MMTrack_bytetrack':
            from pose_pipeline.wrappers.mmtrack import mmtrack_bounding_boxes
            tracks = mmtrack_bounding_boxes(video, 'bytetrack')
            key['tracks'] = tracks

        elif (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') == 'FairMOT':
            from pose_pipeline.wrappers.fairmot import fairmot_bounding_boxes
            tracks = fairmot_bounding_boxes(video)
            key['tracks'] = tracks

        elif (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') == 'TransTrack':
            from pose_pipeline.wrappers.transtrack import transtrack_bounding_boxes
            tracks = transtrack_bounding_boxes(video)
            key['tracks'] = tracks

        elif (TrackingBboxMethodLookup & key).fetch1('tracking_method_name') == 'TraDeS':
            from pose_pipeline.wrappers.trades import trades_bounding_boxes
            tracks = trades_bounding_boxes(video)
            key['tracks'] = tracks

        else:
            os.remove(video)
            raise Exception(f"Unsupported tracking method: {key['tracking_method']}")

        track_ids = np.unique([t['track_id'] for track in tracks for t in track])
        key['num_tracks'] = len(track_ids)

        self.insert1(key)

        # remove the downloaded video to avoid clutter
        if os.path.exists(video):
            os.remove(video)


@schema
class TrackingBboxVideo(dj.Computed):
    definition = '''
    -> BlurredVideo
    -> TrackingBbox
    ---
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):

        import matplotlib
        from pose_pipeline.utils.visualization import video_overlay

        video = (BlurredVideo & key).fetch1('output_video')
        tracks = (TrackingBbox & key).fetch1('tracks')

        N = len(np.unique([t['track_id'] for track in tracks for t in track]))
        colors = matplotlib.cm.get_cmap('hsv', lut=N)

        def overlay_callback(image, idx):
            image = image.copy()

            for track in tracks[idx]:
                c = colors(track['track_id'])
                c = (int(c[0] * 255.0), int(c[1] * 255.0), int(c[2] * 255.0))

                small = int(5e-3 * np.max(image.shape))
                large = 2 * small

                bbox = track['tlbr']
                cv2.rectangle(image, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), (255, 255, 255), large)
                cv2.rectangle(image, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), c, small)

                label = str(track['track_id'])
                textsize = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, int(5.0e-3 * image.shape[0]), 4)[0]
                x = int((bbox[0] + bbox[2]) / 2 - textsize[0] / 2)
                y = int((bbox[3] + bbox[1]) / 2 + textsize[1] / 2)
                cv2.putText(image, label, (x, y), 0, 5.0e-3 * image.shape[0], (255, 255, 255), thickness=large)
                cv2.putText(image, label, (x, y), 0, 5.0e-3 * image.shape[0], c, thickness=small)

            return image

        fd, fname = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)
        video_overlay(video, fname, overlay_callback, downsample=1)

        key['output_video'] = fname

        self.insert1(key)

        # remove the downloaded video to avoid clutter
        os.remove(video)
        os.remove(fname)


@schema
class PersonBboxValid(dj.Manual):
    definition = '''
    -> TrackingBbox
    video_subject_id        : int
    ---
    keep_tracks             : longblob
    '''


@schema
class PersonBbox(dj.Computed):
    definition = '''
    -> PersonBboxValid
    ---
    bbox               : longblob
    present            : longblob
    '''

    def make(self, key):

        tracks = (TrackingBbox & key).fetch1('tracks')
        keep_tracks = (PersonBboxValid & key).fetch1('keep_tracks')

        def extract_person_track(tracks):

            def process_timestamp(track_timestep):
                valid = [t for t in track_timestep if t['track_id'] in keep_tracks]
                if len(valid) == 1:
                    return {'present': True, 'bbox': valid[0]['tlhw']}
                else:
                    return {'present': False, 'bbox': [0.0, 0.0, 0.0, 0.0]}

            return [process_timestamp(t) for t in tracks]

        LD = main_track = extract_person_track(tracks)
        dict_lists = {k: [dic[k] for dic in LD] for k in LD[0]}

        present = np.array(dict_lists['present'])
        bbox =  np.array(dict_lists['bbox'])

        # smooth any brief missing frames
        df = pd.DataFrame(bbox)
        df.iloc[~present] = np.nan
        df = df.fillna(method='bfill', axis=0, limit=2)
        df = df.fillna(method='ffill', axis=0, limit=2)

        # get smoothed version
        key['present'] = ~df.isna().any(axis=1).values
        key['bbox'] = df.values

        self.insert1(key)

    @staticmethod
    def get_overlay_fn(key):

        bboxes = (PersonBbox & key).fetch1('bbox')

        def overlay_fn(image, idx, width=6, color=(255, 255, 255)):
            bbox = bboxes[idx].copy()
            bbox[2:] = bbox[:2] + bbox[2:]
            if np.any(np.isnan(bbox)):
                return image

            cv2.rectangle(image, (int(bbox[0]), int(bbox[1])), (int(bbox[2]), int(bbox[3])), color, width)
            return image

        return overlay_fn

    @property
    def key_source(self):
        return PersonBboxValid & 'video_subject_id >= 0'


@schema
class DetectedFrames(dj.Computed):
    definition = '''
    -> PersonBboxValid
    -> VideoInfo
    ---
    frames_detected        : int
    frames_missed          : int
    fraction_found         : float
    mean_other_people      : float
    median_confidence      : float
    frame_data             : longblob
    '''

    def make(self, key):

        if (PersonBboxValid & key).fetch1('video_subject_id') < 0:
            key['frames_detected'] = 0
            key['frames_missed'] = (VideoInfo & key).fetch1('num_frames')

        # compute statistics
        tracks = (TrackingBbox & key).fetch1('tracks')
        keep_tracks = (PersonBboxValid & key).fetch1('keep_tracks')

        def extract_person_stats(tracks):

            def process_timestamp(track_timestep):
                valid = [t for t in track_timestep if t['track_id'] in keep_tracks]
                total_tracks = len(track_timestep)
                if len(valid) == 1:
                    if 'confidence' in valid[0].keys():
                        return {'present': True, 'confidence': valid[0]['confidence'], 'others': total_tracks-1}
                    else:
                        return {'present': True, 'confidence': 1.0, 'others': total_tracks-1}
                else:
                    return {'present': False, 'confidence': 0, 'others': total_tracks}

            return [process_timestamp(t) for t in tracks]

        stats = extract_person_stats(tracks)
        present = np.array([x['present'] for x in stats])

        key['frames_detected'] = np.sum(present)
        key['frames_missed'] = np.sum(~present)
        key['fraction_found'] = key['frames_detected'] / (key['frames_missed'] + key['frames_detected'])

        if key['frames_detected'] > 0:
            key['median_confidence'] = np.median([x['confidence'] for x in stats if x['present']])
        else:
            key['median_confidence'] = 0.0
        key['mean_other_people'] = np.nanmean([x['others'] for x in stats])
        key['frame_data'] = stats

        self.insert1(key)

    @property
    def key_source(self):
        return PersonBboxValid & 'video_subject_id >= 0'

@schema
class BestDetectedFrames(dj.Computed):
    definition = '''
    -> DetectedFrames
    '''

    def make(self, key):
        detected_frames = (DetectedFrames & key).fetch('fraction_found', 'KEY', as_dict=True)

        best = np.argmax([d['fraction_found'] for d in detected_frames])
        res = detected_frames[best]
        res.pop('fraction_found')
        self.insert1(res)

    @property
    def key_source(self):
        return Video & DetectedFrames

@schema
class OpenPosePerson(dj.Computed):
    definition = '''
    -> PersonBbox
    -> OpenPose
    ---
    keypoints        : longblob
    hand_keypoints   : longblob
    openpose_ids     : longblob
    '''

    def make(self, key):

        # fetch data
        keypoints, hand_keypoints = (OpenPose & key).fetch1('keypoints', 'hand_keypoints')
        bbox = (PersonBbox & key).fetch1('bbox')

        res = [match_keypoints_to_bbox(bbox[idx], keypoints[idx]) for idx in range(bbox.shape[0])]
        keypoints, openpose_ids = list(zip(*res))

        keypoints = np.array(keypoints)
        openpose_ids = np.array(openpose_ids)

        key['keypoints'] = keypoints
        key['openpose_ids'] = openpose_ids

        key['hand_keypoints'] = []

        for openpose_id, hand_keypoint in zip(openpose_ids, hand_keypoints):
            if openpose_id is None:
                key['hand_keypoints'].append(np.zeros((2, 21, 3)))
            else:
                key['hand_keypoints'].append([hand_keypoint[0][openpose_id], hand_keypoint[1][openpose_id]])
        key['hand_keypoints'] = np.asarray(key['hand_keypoints'])

        self.insert1(key)


@schema
class OpenPosePersonVideo(dj.Computed):
    definition = '''
    -> OpenPosePerson
    -> BlurredVideo
    ---
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):

        from pose_pipeline.utils.visualization import video_overlay, draw_keypoints

        # fetch data
        keypoints, hand_keypoints = (OpenPosePerson & key).fetch1('keypoints', 'hand_keypoints')
        video_filename = (BlurredVideo & key).fetch1('output_video')

        fd, fname = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)

        video = (BlurredVideo & key).fetch1('output_video')
        keypoints = (OpenPosePerson & key).fetch1('keypoints')

        def overlay(image, idx):
            image = draw_keypoints(image, keypoints[idx])
            image = draw_keypoints(image, hand_keypoints[idx, 0], threshold=0.02)
            image = draw_keypoints(image, hand_keypoints[idx, 1], threshold=0.02)
            return image

        ofd, out_file_name = tempfile.mkstemp(suffix='.mp4')
        os.close(ofd)
        video_overlay(video, out_file_name, overlay, downsample=4)
        key['output_video'] = out_file_name

        self.insert1(key)

        os.remove(out_file_name)
        os.remove(video)


@schema
class TopDownMethodLookup(dj.Lookup):
    definition = '''
    top_down_method      : int
    ---
    top_down_method_name : varchar(50)
    '''
    contents = [
        {'top_down_method': 0, 'top_down_method_name': 'MMPose'},
        {'top_down_method': 1, 'top_down_method_name': 'MMPoseWholebody'}]


@schema
class TopDownMethod(dj.Manual):
    definition = '''
    -> PersonBbox
    top_down_method    : int
    '''


@schema
class TopDownPerson(dj.Computed):
    definition = '''
    -> TopDownMethod
    ---
    keypoints          : longblob
    '''

    def make(self, key):

        if (TopDownMethodLookup & key).fetch1('top_down_method_name') == 'MMPose':
            from .wrappers.mmpose import mmpose_top_down_person
            key['keypoints'] = mmpose_top_down_person(key)
        elif (TopDownMethodLookup & key).fetch1('top_down_method_name') == 'MMPoseWholebody':
            from .wrappers.mmpose import mmpose_whole_body
            key['keypoints'] = mmpose_whole_body(key)
        else:
            raise Exception("Method not implemented")

        self.insert1(key)

    @staticmethod
    def joint_names():
        return ["Nose", "Left Eye", "Right Eye", "Left Ear", "Right Ear", "Left Shoulder", "Right Shoulder",
                "Left Elbow", "Right Elbow", "Left Wrist", "Right Wrist", "Left Hip", "Right Hip", "Left Knee",
                "Right Knee", "Left Ankle", "Right Ankle"]


@schema
class SkeletonAction(dj.Computed):
    definition = '''
    -> TopDownPerson
    method            : varchar(50)
    ---
    top5              : longblob
    action_scores     : longblob
    label_map         : longblob
    action_window_len : int
    stride            : int
    computed_timestamp=CURRENT_TIMESTAMP : timestamp    # automatic timestamp
    '''

    # Note: this will likely be refactored with a lookup table in the near future
    # to support different methods
    def make(self, key):

        from pose_pipeline.wrappers.mmaction import mmaction_skeleton_action_person
        key = mmaction_skeleton_action_person(key, stride=1)
        key['method'] = 'mmaction_skeleton'
        self.insert1(key)


@schema
class SkeletonActionVideo(dj.Computed):
    definition = '''
    -> SkeletonAction
    ---
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):
        from pose_pipeline.utils.visualization import video_overlay, draw_keypoints

        video = (BlurredVideo & key).fetch1('output_video')
        keypoints = (TopDownPerson & key).fetch1('keypoints')

        bbox_fn = PersonBbox.get_overlay_fn(key)
        bbox = (PersonBbox & key).fetch1('bbox')

        top5_actions, stride = (SkeletonAction & key).fetch1('top5', 'stride')

        def overlay_fn(image, idx):
            image = draw_keypoints(image, keypoints[idx], radius=20, color=(0, 255, 0))
            image = bbox_fn(image, idx, width=14, color=(0, 0, 255))

            if np.any(np.isnan(bbox[idx])):
                return image

            top5 = top5_actions[min(len(top5_actions)-1, idx // stride)]

            top_left = bbox[idx, :2]
            for i, (action, score) in enumerate(top5):
                if score > 0.1:
                    label = f'{action.capitalize()}: {score:0.3}'

                    fontsize = 1.0e-3 * image.shape[0]
                    textsize = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fontsize, 4)[0]

                    coord = (int(top_left[0] + 5), int(top_left[1] + (10 + textsize[1]) * (1+i)))
                    cv2.putText(image, label, coord, 0, fontsize, (255, 255, 255), thickness=4)

            return image

        fd, out_file_name = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)
        video_overlay(video, out_file_name, overlay_fn, downsample=1)

        key['output_video'] = out_file_name

        self.insert1(key)

        os.remove(out_file_name)
        os.remove(video)

        return out_file_name


@schema
class LiftingMethodLookup(dj.Lookup):
    definition = '''
    lifting_method      : int
    ---
    lifting_method_name : varchar(50)
    '''
    contents = [
        {'lifting_method': 0, 'lifting_method_name': 'GastNet'},
        {'lifting_method': 1, 'lifting_method_name': 'VideoPose3D'},
        {'lifting_method': 2, 'lifting_method_name': 'PoseAug'},

    ]


@schema
class LiftingMethod(dj.Manual):
    definition = '''
    -> TopDownPerson
    -> LiftingMethodLookup
    '''


@schema
class LiftingPerson(dj.Computed):
    definition = '''
    -> LiftingMethod
    ---
    keypoints_3d       : longblob
    keypoints_valid    : longblob
    '''

    def make(self, key):

        if (LiftingMethodLookup & key).fetch1('lifting_method_name') == 'RIE':
            from .wrappers.rie_lifting import process_rie
            results = process_rie(key)
        elif (LiftingMethodLookup & key).fetch1('lifting_method_name') == 'GastNet':
            from .wrappers.gastnet_lifting import process_gastnet
            results = process_gastnet(key)
        elif (LiftingMethodLookup & key).fetch1('lifting_method_name') == 'VideoPose3D':
            from .wrappers.videopose3d import process_videopose3d
            results = process_videopose3d(key)
        elif (LiftingMethodLookup & key).fetch1('lifting_method_name') == 'PoseAug':
            from .wrappers.poseaug import process_poseaug
            results = process_poseaug(key)
        else:
            raise Exception(f"Method not implemented {key}")

        key.update(results)
        self.insert1(key)

    def joint_names():
        """ Lifting layers use Human3.6 ordering """
        return ['Hip (root)', 'Right hip', 'Right knee', 'Right foot', 'Left hip', 'Left knee', 'Left foot', 'Spine', 'Thorax',
                'Nose', 'Head', 'Left shoulder', 'Left elbow', 'Left wrist', 'Right shoulder', 'Right elbow', 'Right wrist']


@schema
class LiftingPersonVideo(dj.Computed):
    definition = """
    -> LiftingPerson
    -> BlurredVideo
    ---
    output_video      : attach@localattach    # datajoint managed video file
    """

    def make(self, key):

        keypoints = (TopDownPerson & key).fetch1('keypoints')
        keypoints_3d = (LiftingPerson & key).fetch1('keypoints_3d').copy()
        blurred_video = (BlurredVideo & key).fetch1('output_video')
        width, height, fps = (VideoInfo & key).fetch1('width', 'height', 'fps')
        fd, out_file_name = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)

        with add_path(os.environ["GAST_PATH"]):

            from common.graph_utils import adj_mx_from_skeleton
            from common.skeleton import Skeleton
            from tools.inference import gen_pose
            from tools.preprocess import h36m_coco_format, revise_kpts

            from tools.vis_h36m import render_animation

            skeleton = Skeleton(parents=[-1, 0, 1, 2, 0, 4, 5, 0, 7, 8, 9, 8, 11, 12, 8, 14, 15],
                                joints_left=[6, 7, 8, 9, 10, 16, 17, 18, 19, 20, 21, 22, 23],
                                joints_right=[1, 2, 3, 4, 5, 24, 25, 26, 27, 28, 29, 30, 31])
            adj = adj_mx_from_skeleton(skeleton)

            joints_left, joints_right = [4, 5, 6, 11, 12, 13], [1, 2, 3, 14, 15, 16]
            kps_left, kps_right = [4, 5, 6, 11, 12, 13], [1, 2, 3, 14, 15, 16]
            rot = np.array([0.14070565, -0.15007018, -0.7552408, 0.62232804], dtype=np.float32)
            keypoints_metadata = {'keypoints_symmetry': (joints_left, joints_right), 'layout_name': 'Human3.6M', 'num_joints': 17}

            keypoints_reformat, keypoints_score = keypoints[None, ..., :2], keypoints[None, ..., 2]
            keypoints, scores, valid_frames = h36m_coco_format(keypoints_reformat, keypoints_score)
            re_kpts = revise_kpts(keypoints, scores, valid_frames)
            re_kpts = re_kpts.transpose(1, 0, 2, 3)

            keypoints_3d[:, :, 2] -= np.amin(keypoints_3d[:, :, 2])
            anim_output = {'Reconstruction 1': keypoints_3d}

            render_animation(re_kpts, keypoints_metadata, anim_output, skeleton, fps, 30000, np.array(70., dtype=np.float32),
                            out_file_name, input_video_path=blurred_video, viewport=(width, height), com_reconstrcution=False)

        key['output_video'] = out_file_name
        self.insert1(key)

        os.remove(blurred_video)
        os.remove(out_file_name)


## Classes that handle SMPL meshed based tracking
@schema
class SMPLMethodLookup(dj.Lookup):
    definition = '''
    smpl_method       : int
    ---
    smpl_method_name  : varchar(50)
    '''
    contents = [{'smpl_method': 0, 'smpl_method_name': 'VIBE'},
                {'smpl_method': 1, 'smpl_method_name': 'MEVA'},
                {'smpl_method': 2, 'smpl_method_name': "ProHMR"},
                {'smpl_method': 3, 'smpl_method_name': "Expose"},
                {'smpl_method': 4, 'smpl_method_name': "PARE"},
                {'smpl_method': 5, 'smpl_method_name': "PIXIE"},
                {'smpl_method': 6, 'smpl_method_name': "ProHMR_MMPose"}]


@schema
class SMPLMethod(dj.Manual):
    definition = '''
    -> PersonBbox
    -> SMPLMethodLookup
    '''


@schema
class SMPLPerson(dj.Computed):
    definition = '''
    -> SMPLMethod
    ---
    model_type      : varchar(50)
    cams            : longblob
    poses           : longblob
    betas           : longblob
    joints3d        : longblob
    joints2d        : longblob
    '''

    #verts           : longblob

    def make(self, key):

        smpl_method_name = (SMPLMethodLookup & key).fetch1('smpl_method_name')
        if smpl_method_name == 'VIBE':

            from .wrappers.vibe import process_vibe
            res = process_vibe(key)
            res['model_type'] = 'SMPL'

        elif smpl_method_name == 'MEVA':

            from .wrappers.meva import process_meva
            res = process_meva(key)
            res['model_type'] = 'SMPL'

        elif smpl_method_name == 'ProHMR':

            from .wrappers.prohmr import process_prohmr
            res = process_prohmr(key)
            res['model_type'] = 'SMPL'

        elif smpl_method_name == 'ProHMR_MMPose':
            from .wrappers.prohmr import process_prohmr_mmpose
            res = process_prohmr_mmpose(key)
            res['model_type'] = 'SMPL'

        elif smpl_method_name == 'Expose':

            from .wrappers.expose import process_expose
            res = process_expose(key)
            res['model_type'] = 'SMPL-X'

        elif smpl_method_name == 'PARE':

            from .wrappers.pare import process_pare
            res = process_pare(key)
            res['model_type'] = 'SMPL'

        elif smpl_method_name == 'PIXIE':

            from .wrappers.pixie import process_pixie
            res = process_pixie(key)
            res['model_type'] = 'SMPL-X'

        else:
            raise Exception(f"Method {smpl_method_name} not implemented")

        if 'verts' in res.keys():
            res.pop('verts')

        self.insert1(res)

    @staticmethod
    def joint_names(model='smpl'):
        if model.upper() == 'SMPL':
            from .utils.smpl import JOINT_NAMES_49
            return JOINT_NAMES_49
        elif model.upper() in ['SMPLX', 'SMPL-X']:
            from smplx.joint_names import JOINT_NAMES
            return JOINT_NAMES
        elif model.upper() == 'PIXIE':
            # frustratingly, Pixie does not use the default keypoint ordering
            # TODO: can likely remove the cfg.model.extra_joint_path setting and get defaults
            with add_path(os.environ['PIXIE_PATH']):
                from pixielib.models.SMPLX import SMPLX_names as pixie_joint_names
            return pixie_joint_names


    @staticmethod
    def smpl_joint_names(model='smpl'):
        from smplx.joint_names import JOINT_NAMES

        if model == 'smpl':
            return JOINT_NAMES[:19]
        elif model == 'smplx':
            # in smplx models the pelvis orientation is in a different field (global orientation)
            # as are the wrists
            return JOINT_NAMES[1:21]
        elif model == 'PIXIE':
            # in addition to the dropped fields for smplx, Pixie also splits out the head and neck
            # into additional fields
            return [j for j in JOINT_NAMES[:20] if j not in ['pelvis', 'head', 'neck']]
        else:
            raise Exception('Unknown model type')



@schema
class SMPLPersonVideo(dj.Computed):
    definition = '''
    -> SMPLPerson
    -> BlurredVideo
    ---
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):

        from pose_pipeline.utils.visualization import video_overlay

        poses, betas, cams = (SMPLPerson & key).fetch1('poses', 'betas', 'cams')

        smpl_method_name = (SMPLMethodLookup & key).fetch1('smpl_method_name')
        if smpl_method_name == 'ProHMR' or smpl_method_name == 'ProHMR_MMPose':
            from .wrappers.prohmr import get_prohmr_smpl_callback
            callback = get_prohmr_smpl_callback(key, poses, betas, cams)
        elif smpl_method_name == 'Expose':
            from .wrappers.expose import get_expose_callback
            callback = get_expose_callback(key)
        elif smpl_method_name == 'PIXIE':
            from .wrappers.pixie import get_pixie_callback
            callback = get_pixie_callback(key)
        else:
            from pose_pipeline.utils.visualization import get_smpl_callback
            callback = get_smpl_callback(key, poses, betas, cams)

        video = (BlurredVideo & key).fetch1('output_video')

        fd, out_file_name = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)
        video_overlay(video, out_file_name, callback, downsample=1)
        key['output_video'] = out_file_name

        self.insert1(key)

        os.remove(video)



@schema
class CenterHMR(dj.Computed):
    definition = '''
    -> Video
    ---
    results           : longblob
    '''

    def make(self, key):

        with add_path([os.path.join(os.environ['CENTERHMR_PATH'], 'src'),
                       os.path.join(os.environ['CENTERHMR_PATH'], 'src/core')]):
            from pose_pipeline.wrappers.centerhmr import centerhmr_parse_video

            video = Video.get_robust_reader(key, return_cap=False)
            res = centerhmr_parse_video(video, os.environ['CENTERHMR_PATH'])

        # don't store verticies or images
        keys_to_keep = ['params',  'pj2d', 'j3d', 'j3d_smpl24', 'j3d_spin24', 'j3d_op25']
        res = [{k: v for k, v in r.items() if k in keys_to_keep} for r in res]
        key['results'] = res

        self.insert1(key)

        # not saving the video in database, just to reduce space requirements
        os.remove(video)


@schema
class CenterHMRPerson(dj.Computed):
    definition = '''
    -> PersonBbox
    -> CenterHMR
    -> VideoInfo
    ---
    keypoints        : longblob
    poses            : longblob
    betas            : longblob
    cams             : longblob
    global_orients   : longblob
    centerhmr_ids    : longblob
    '''

    def make(self, key):

        width, height = (VideoInfo & key).fetch1('width', 'height')

        def convert_keypoints_to_image(keypoints, imsize=[width, height]):
            mp = np.array(imsize) * 0.5
            scale = np.max(np.array(imsize)) * 0.5

            keypoints_image = keypoints * scale + mp
            return list(keypoints_image)

        # fetch data
        hmr_results = (CenterHMR & key).fetch1('results')
        bbox = (PersonBbox & key).fetch1('bbox')

        # get the 2D keypoints. note these are scaled from (-0.5, 0.5) assuming a
        # square image (hence convert_keypoints_to_image)
        pj2d = [r['pj2d'] if 'pj2d' in r.keys() else np.zeros((0, 25, 2)) for r in hmr_results]
        all_matches = [match_keypoints_to_bbox(bbox[idx], convert_keypoints_to_image(pj2d[idx]), visible=False)
                       for idx in range(bbox.shape[0])]

        keypoints, centerhmr_ids = list(zip(*all_matches))

        key['poses'] = np.asarray([res['params']['body_pose'][id]
                                   if id is not None else np.array([np.nan] * 69) * np.nan
                                   for res, id in zip(hmr_results, centerhmr_ids)])
        key['betas'] = np.asarray([res['params']['betas'][id]
                                   if id is not None else np.array([np.nan] * 10) * np.nan
                                   for res, id in zip(hmr_results, centerhmr_ids)])
        key['cams'] = np.asarray([res['params']['cam'][id]
                                  if id is not None else np.array([np.nan] * 3) * np.nan
                                  for res, id in zip(hmr_results, centerhmr_ids)])
        key['global_orients'] = np.asarray([res['params']['global_orient'][id]
                                            if id is not None else np.array([np.nan] * 3) * np.nan
                                            for res, id in zip(hmr_results, centerhmr_ids)])

        key['keypoints'] = np.asarray(keypoints)
        key['centerhmr_ids'] = np.asarray(centerhmr_ids)

        self.insert1(key)

    @staticmethod
    def joint_names():
        from smplx.joint_names import JOINT_NAMES
        return JOINT_NAMES[:23]


@schema
class CenterHMRPersonVideo(dj.Computed):
    definition = '''
    -> CenterHMRPerson
    -> BlurredVideo
    ---
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):

        from pose_estimation.util.pyrender_renderer import PyrendererRenderer
        from pose_estimation.body_models.smpl import SMPL
        from pose_pipeline.utils.visualization import video_overlay

        # fetch data
        pose_data = (CenterHMRPerson & key).fetch1()
        video_filename = (BlurredVideo & key).fetch1('output_video')

        fd, fname = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)

        video = (BlurredVideo & key).fetch1('output_video')

        smpl = SMPL()

        def overlay(image, idx):
            body_pose = np.concatenate([pose_data['global_orients'][idx], pose_data['poses'][idx]])
            body_beta = pose_data['betas'][idx]

            if np.any(np.isnan(body_pose)):
                return image

            h, w = image.shape[:2]
            if overlay.renderer is None:
                overlay.renderer = PyrendererRenderer(smpl.get_faces(), (h, w))

            verts = smpl(body_pose.astype(np.float32)[None, ...], body_beta.astype(np.float32)[None, ...])[0][0]

            cam = [pose_data['cams'][idx][0], *pose_data['cams'][idx][:3]]
            if h > w:
                cam[0] = 1.1 ** cam[0] * (h / w)
                cam[1] = (1.1 ** cam[1])
            else:
                cam[0] = 1.1 ** cam[0]
                cam[1] = (1.1 ** cam[1]) * (w / h)

            return overlay.renderer(verts, cam, img=image)
        overlay.renderer = None

        ofd, out_file_name = tempfile.mkstemp(suffix='.mp4')
        os.close(ofd)
        video_overlay(video, out_file_name, overlay, downsample=4)
        key['output_video'] = out_file_name

        self.insert1(key)

        os.remove(out_file_name)
        os.remove(video)


@schema
class HumorPerson(dj.Computed):
    definition = '''
    -> OpenPosePerson
    ----
    trans         : longblob
    root_orient   : longblob
    pose_body     : longblob
    betas         : longblob
    latent_pose   : longblob
    latent_motion : longblob
    floor_plane   : longblob
    contacts      : longblob
    vertices      : longblob
    faces         : longblob
    '''

    def make(self, key):

        from pose_pipeline.wrappers.humor import process_humor

        res = process_humor(key)

        self.insert1(res)


@schema
class HumorPersonVideo(dj.Computed):
    definition = '''
    -> HumorPerson
    ----
    output_video      : attach@localattach    # datajoint managed video file
    '''

    def make(self, key):

        from pose_pipeline.wrappers.humor import render_humor

        video = render_humor(key)
        key['output_video'] = video

        self.insert1(key)


@schema
class TopDownPersonVideo(dj.Computed):
    definition = """
    -> TopDownPerson
    -> BlurredVideo
    ---
    output_video      : attach@localattach    # datajoint managed video file
    """

    def make(self, key):

        from pose_pipeline.utils.visualization import video_overlay, draw_keypoints

        video = (BlurredVideo & key).fetch1('output_video')
        keypoints = (TopDownPerson & key).fetch1('keypoints')

        bbox_fn = PersonBbox.get_overlay_fn(key)

        def overlay_fn(image, idx):
            image = draw_keypoints(image, keypoints[idx])
            image = bbox_fn(image, idx)
            return image

        fd, out_file_name = tempfile.mkstemp(suffix='.mp4')
        os.close(fd)
        video_overlay(video, out_file_name, overlay_fn, downsample=1)

        key['output_video'] = out_file_name

        self.insert1(key)

        os.remove(out_file_name)
        os.remove(video)

    @staticmethod
    def joint_names():
        """ PoseFormer follows the output format of Video3D and uses Human3.6 ordering """
        return ['Hip (root)', 'Right hip', 'Right knee', 'Right foot', 'Left hip', 'Left knee', 'Left foot', 'Spine', 'Thorax', 'Nose', 'Head', 'Left shoulder', 'Left elbow', 'Left wrist', 'Right shoulder', 'Right elbow', 'Right wrist']

