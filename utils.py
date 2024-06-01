'''
Copyright (C) 2024 Ian Sloat

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
'''
import os
import bpy
from . import MotionEngine as me


def prepare_camera_for_clip(movie_clip: bpy.types.MovieClip, context: bpy.types.Context):
    """
    Ensure scene cameras exist for movie clip
    :param movie_clip: Reference clip
    :param context: Context from operator
    :return: A camera object with matching settings for sensor size and focal length.
    """
    scene = context.scene
    scene_root = scene.collection
    cam_name = movie_clip.name

    cam_data = bpy.data.cameras.get(cam_name)
    if cam_data is None:
        cam_data = bpy.data.cameras.new(cam_name)

    cam_obj = None
    for obj in scene.objects:
        if obj.data is cam_data:
            cam_obj = obj
            break
    if cam_obj is None:
        cam_obj = bpy.data.objects.new(cam_name, object_data=cam_data)

    if cam_obj.name not in scene_root.objects:
        scene_root.objects.link(cam_obj)

    cam_data.background_images.clear()

    cam_bg_img = cam_data.background_images.new()
    cam_bg_img.clip = movie_clip
    cam_bg_img.display_depth = 'FRONT'
    cam_bg_img.source = 'MOVIE_CLIP'
    cam_bg_img.alpha = 0.75
    cam_bg_img.clip_user.use_render_undistorted = True
    cam_data.show_background_images = True
    cam_bg_img.show_expanded = False

    cam_data.sensor_width = movie_clip.tracking.camera.sensor_width
    cam_data.lens = movie_clip.tracking.camera.focal_length

    return cam_obj


_pose_sources = None


def update_pose_sources():
    global _pose_sources
    models = me.get_models('pose_estimation', ['target_class', 'keypoints'])
    classes = list({m_attr['target_class'] for (m_name, m_attr) in models})
    kp_nums = list({m_attr['keypoints'] for (m_name, m_attr) in models})
    _pose_sources = []
    for cl in classes:
        for kpn in kp_nums:
            _pose_sources.append(f'{cl}{kpn}')


def get_pose_sources():
    if _pose_sources is None:
        update_pose_sources()
    return _pose_sources


def is_valid_joint_name(name: str):
    """
    Checks if the provided input string meets the naming standards for tracks generated by the pose detector

    The naming standards are defined as:

    Src_Det.pose_source.ID

    Where Src_Det can be any sequence of characters, including ones that have . separators

    When separated by the '.' character, the number of resulting elements must be 3 or greater,
    with the final 2 elements comprised of

    [pose_source, ID]

    Where pose_source is a model's 'target_class' and 'keypoints' attribute concatenated together,
    and ID is an integer representing the joint's ID within the pose

    :param name: The name to test
    :return: True if the name meets these requirements
    """
    pose_sources = get_pose_sources()
    split_name = name.split('.')
    format_check = len(split_name) >= 3 and split_name[-2] in pose_sources
    value_check = format_check
    if value_check:
        try:
            int(split_name[-1])
        except ValueError:
            value_check = False
    return format_check and value_check


def get_joint_tracks(movie_clip: bpy.types.MovieClip, filter_locked=False):
    """
    Create a dictionary of all tracks that qualify as pose joints

    as defined by new spec

    Src_Det.pose_source.ID

    Src_Det can be any string, including strings that have . separators

    The only requirement is that the last two elements of the string equal

    [pose_source, ID]

    :param movie_clip: Clip to scan for tracks
    :param filter_locked: If true, only use locked tracks for tracking data
    :return: Dictionary that maps Src_Det name to a dictionary of joint ids and joints
     as well as a list of qualifying tracks
    """
    track_dict = {}
    tracks = []

    for track in movie_clip.tracking.tracks:
        if not is_valid_joint_name(track.name):
            continue
        if filter_locked and not track.lock:
            continue
        split_name = track.name.split('.')
        joint_id = int(split_name[-1])
        pose_name = ''
        for i in range(len(split_name) - 2):
            if pose_name != '':
                pose_name += '.' + split_name[i]
            else:
                pose_name = split_name[i]
        if pose_name not in track_dict:
            track_dict[pose_name] = {}
        if split_name[-2] not in track_dict[pose_name]:
            track_dict[pose_name][split_name[-2]] = {}
        track_dict[pose_name][split_name[-2]][joint_id] = track
        tracks.append(track)

    return track_dict, tracks


def get_marker_dims(marker: bpy.types.MovieTrackingMarker, parent_clip_size: tuple[int, int]):
    co = marker.co
    corners = marker.pattern_corners
    corners = [(parent_clip_size[0] * (co[0] + x), parent_clip_size[1] * (co[1] + y)) for (x, y) in corners]
    min_x = min([x for (x, y) in corners])
    max_x = max([x for (x, y) in corners])
    min_y = min([y for (x, y) in corners])
    max_y = max([y for (x, y) in corners])
    width = abs(max_x - min_x)
    height = abs(max_y - min_y)
    return (min_x, min_y), (max_x, max_y), width, height


def get_marker_area(marker: bpy.types.MovieTrackingMarker, parent_clip_size: tuple[int, int], get_exact=False):
    (_, min_y), _, width, height = get_marker_dims(marker, parent_clip_size)
    if get_exact:
        co = marker.co
        corners = marker.pattern_corners
        corners = [(parent_clip_size[0] * (co[0] + x), parent_clip_size[1] * (co[1] + y)) for (x, y) in corners]
        areas = []
        for i in range(len(corners)):
            c_a = corners[i % len(corners)]
            c_b = corners[(i + 1) % len(corners)]
            width = c_b[0] - c_a[0]
            height = ((c_a[1] - min_y) + (c_b[1] - min_y)) / 2
            areas.append(width * height)
        return abs(sum(areas))
    return width * height


def get_clip_poses(movie_clip: bpy.types.MovieClip, joint_conf_thresh=0.9, filter_locked=False):
    """
    Get all named poses on the provided clip, converting them to a format compatible with
    MotionEngine.tracking.TrackData
    :param movie_clip: Clip to extract data from
    :param joint_conf_thresh: Joint confidence threshold
    :param filter_locked: If true, only use locked tracks for tracking data
    :return: Nested dictionary with mappings to frames and named poses. Frames will be in scene time.
    """
    output = {}
    track_dict, tracks = get_joint_tracks(movie_clip, filter_locked)
    clip_info = ClipInfo(movie_clip)
    clip_size = movie_clip.size
    for pose_name, sources in track_dict.items():
        for source, joints in sources.items():
            actual_pose_name = f'{pose_name}.{source}'
            for joint, joint_track in joints.items():
                for marker in joint_track.markers:
                    scene_frame = clip_info.clip_to_scene(marker.frame)
                    conf = min(100.0, max(0.0, get_marker_area(marker, clip_size, True))) / 100.0
                    if conf < joint_conf_thresh:
                        continue
                    x = marker.co[0] * clip_size[0]
                    y = clip_size[1] - marker.co[1] * clip_size[1]
                    if scene_frame not in output:
                        output[scene_frame] = {}
                    if actual_pose_name not in output[scene_frame]:
                        output[scene_frame][actual_pose_name] = me.dnn.Pose()
                    output[scene_frame][actual_pose_name].set_joint(joint, x, y, conf)

    return output


def get_clip_detections(movie_clip: bpy.types.MovieClip, filter_locked=False):
    """
    Get all named object detections on the provided clip, converting them to a format compatible with
    MotionEngine.tracking.TrackData
    :param movie_clip: Clip to extract data from
    :param filter_locked: If true, only use locked tracks for tracking data
    :return: Nested dictionary with mappings to frames and named detections
    """
    output = {}
    clip_info = ClipInfo(movie_clip)
    clip_size = movie_clip.size
    for track in movie_clip.tracking.tracks:
        if filter_locked and not track.lock:
            continue
        if is_valid_joint_name(track.name) or is_valid_tag_name(track.name):
            continue
        for marker in track.markers:
            scene_frame = clip_info.clip_to_scene(marker.frame)
            bl, tr, width, height = get_marker_dims(marker, clip_size)
            tl_x = bl[0]
            tl_y = clip_size[1] - tr[1]
            if scene_frame not in output:
                output[scene_frame] = {}
            output[scene_frame][track.name] = me.dnn.Detection(0, me.Rect(tl_x, tl_y, width, height), 1)
    return output


def is_valid_tag_name(name: str):
    split_name = name.split('.')
    if len(split_name) < 3:
        return False
    valid_sources = [e for e in me.TagDictionary.__members__]
    valid_sources.append('ML')
    valid_id = True
    try:
        int(split_name[2])
    except ValueError:
        valid_id = False
    return valid_id and split_name[0] == 'Tag' and split_name[1] in valid_sources


def marker_to_tag(marker: bpy.types.MovieTrackingMarker, clip_size=(1, 1), fix_corner_order=True):
    width = clip_size[0] if clip_size[0] >= 1 else 1
    height = clip_size[1] if clip_size[1] >= 1 else 1
    norm_center_x, norm_center_y = marker.co
    corners = list(marker.pattern_corners)
    if fix_corner_order:
        corners.reverse()
    corners = [(x + norm_center_x, y + norm_center_y) for (x, y) in corners]
    corners = [(x * clip_size[0], clip_size[1] - (y * clip_size[1])) for (x, y) in corners]
    new_tag = me.dnn.Tag()
    for c in range(4):
        new_tag[c] = corners[c]
    return new_tag


def get_clip_tags(movie_clip: bpy.types.MovieClip, filter_locked=False):
    """
    Get all tag detections on the provided clip
    :param movie_clip: Clip to extract data from
    :param filter_locked: If true, only use locked tracks for tracking data
    :return: Nested dictionary with mappings to frames and tags
    """
    return {}


def get_clip_tracking_data(movie_clip: bpy.types.MovieClip, pose_joint_conf=0.9, include_poses=True,
                           include_detections=True, include_tags=True, filter_locked=False):
    """
    Get all clip tracking data and prepare it for use with MEPython
    :param movie_clip: Clip to extract data from
    :param pose_joint_conf: Pose joint confidence threshold
    :param include_poses: If true, extract pose data
    :param include_detections: If true, extract object detections
    :param include_tags: If true, extract tag detections
    :param filter_locked: If true, only use locked tracks for tracking data
    :return: MEPython TrackingData object
    """
    result = me.tracking.TrackingData()
    if include_poses:
        result.poses = get_clip_poses(movie_clip, pose_joint_conf, filter_locked)
    if include_detections:
        result.detections = get_clip_detections(movie_clip, filter_locked)
    if include_tags:
        result.tags = get_clip_tags(movie_clip, filter_locked)
    return result


def get_clip_Kk(movie_clip: bpy.types.MovieClip):
    """
    Create an MEPython camera intrinsics Kk object from camera information in a blender movie clip
    :param movie_clip: Clip to retrieve camera information from
    :return: MEPython Kk camera intrinsics object
    """
    cam_Kk = me.tracking.Kk()
    clip_cam_settings = movie_clip.tracking.camera
    clip_size = movie_clip.size

    # Set distortion coefficients
    cam_Kk.k[0] = clip_cam_settings.k1
    cam_Kk.k[1] = clip_cam_settings.k2
    cam_Kk.k[2] = clip_cam_settings.k3

    # Calculate focal length in pixels
    fx = clip_cam_settings.focal_length * clip_size[0] / clip_cam_settings.sensor_width
    fy = fx

    # Calculate the principal point coordinates (in pixels)
    cx = clip_size[0] / 2.0
    cy = clip_size[1] / 2.0

    # Copy camera matrix values
    cam_Kk.K[0, 0] = fx
    cam_Kk.K[1, 1] = fy
    cam_Kk.K[0, 2] = cx
    cam_Kk.K[1, 2] = cy

    return cam_Kk


class ClipInfo:
    def __init__(self, clip: bpy.types.MovieClip):
        self.abs_path = os.path.normpath(bpy.path.abspath(clip.filepath))
        self.frame_start = clip.frame_start
        self.frame_offset = clip.frame_offset
        self.source_type = clip.source
        self.clip_size = clip.size

    def get_scene_start(self):
        """
        Returns the scene frame number that the real initial frame will play at
        """
        return self.frame_start - self.frame_offset

    def get_clip_start(self):
        """
        Returns the clip frame number that the real initial frame will play at
        """
        return 1 - self.frame_offset

    def scene_to_true(self, scene_frame: int):
        """
        Converts the provided scene frame number to its equivalent true source frame number.
        Source frames are zero-indexed.
        """
        return scene_frame - self.get_scene_start()

    def clip_to_true(self, clip_frame: int):
        """
        Converts the provided clip frame number to its equivalent true source frame number.
        Source frames are zero-indexed.
        """
        return clip_frame - self.get_clip_start()

    def true_to_scene(self, true_frame: int):
        """
        Converts the provided true source frame number to its equivalent scene frame number.
        Source frames are zero-indexed.
        """
        return true_frame + self.get_scene_start()

    def true_to_clip(self, true_frame: int):
        """
        Converts the provided true source frame number to its equivalent clip frame number.
        Source frames are zero-indexed.
        """
        return true_frame + self.get_clip_start()

    def clip_to_scene(self, clip_frame: int):
        """
        Converts the provided clip frame number to its equivalent scene frame number.
        """
        return self.true_to_scene(self.clip_to_true(clip_frame))

    def scene_to_clip(self, scene_frame: int):
        """
        Converts the provided scene frame number to its equivalent clip frame number.
        """
        return self.true_to_clip(self.scene_to_true(scene_frame))


def get_active_track_count(clip: bpy.types.MovieClip):
    count = 0
    for track in clip.tracking.tracks:
        if is_valid_joint_name(track.name) or is_valid_tag_name(track.name) or not track.select:
            continue
        count += 1
    return count


class ClipTrackingData(ClipInfo):
    def __init__(self, clip: bpy.types.MovieClip):
        super().__init__(clip)

        self.selected_tracks = set()
        self.all_tracks = set()
        self.track_data = {}

        for track in clip.tracking.tracks:
            if is_valid_joint_name(track.name) or is_valid_tag_name(track.name):
                continue
            self.all_tracks.add(track.name)
            if track.select:
                self.selected_tracks.add(track.name)
            for marker in track.markers:
                frame = marker.frame
                bbox = marker.pattern_bound_box
                center = marker.co
                x_bl = (center[0] + bbox[0][0]) * self.clip_size[0]
                y_bl = (center[1] + bbox[0][1]) * self.clip_size[1]
                x_tr = (center[0] + bbox[1][0]) * self.clip_size[0]
                y_tr = (center[1] + bbox[1][1]) * self.clip_size[1]
                true_center = ((x_bl + x_tr) / 2, (y_bl + y_tr) / 2)
                width = abs(x_bl - x_tr)
                height = abs(y_bl - y_tr)
                x_tl = true_center[0] - width / 2
                y_tl = true_center[1] + height / 2
                me_rect = me.Rect(x_tl, self.clip_size[1] - y_tl, width, height)
                if frame not in self.track_data:
                    self.track_data[frame] = {}
                self.track_data[frame][track.name] = me.dnn.Detection(0, me_rect, 1)


def force_ui_draw():
    properties = bpy.context.scene.motion_engine_ui_properties
    if properties.me_ui_redraw_prop:
        properties.me_ui_redraw_prop = False
    else:
        properties.me_ui_redraw_prop = True
